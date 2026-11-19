"""
Tests for ElasticParser entity resolution.

Run from the repo root:
    python -m pytest tests/test_elastic_parser.py -v
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.semantic_parser import ElasticParser, SemanticParser  # noqa: E402


def make_doc(category, action, host_id="host-a", proc=None, ts="2026-11-01T12:00:00.000Z",
             event_id=None, **extra):
    """Build a minimal Elastic Defend-style ECS document."""
    doc = {
        "@timestamp": ts,
        "ecs": {"version": "8.11.0"},
        "event": {"category": [category], "action": action},
        "host": {"id": host_id, "hostname": host_id},
        "process": proc if proc is not None else {
            "entity_id": f"{host_id}-bash-1", "pid": 100, "name": "bash",
            "executable": "/usr/bin/bash",
        },
    }
    if event_id:
        doc["event"]["id"] = event_id
    doc.update(extra)
    return doc


def test_uses_process_entity_id():
    p = ElasticParser()
    ev = p.parse_event(make_doc("file", "open", file={"path": "/etc/passwd"}))
    assert ev.source.entity_id == "host-a-bash-1"
    assert ev.source.attributes["id_source"] == "entity_id"


def test_same_file_on_two_hosts_is_two_nodes():
    p = ElasticParser()
    a = p.parse_event(make_doc("file", "open", host_id="host-a", file={"path": "/etc/passwd"}))
    b = p.parse_event(make_doc("file", "open", host_id="host-b", file={"path": "/etc/passwd"}))
    assert a.target is not b.target
    assert a.target.entity_id == "host-a|/etc/passwd"
    assert b.target.entity_id == "host-b|/etc/passwd"


def test_same_destination_on_two_hosts_is_two_nodes():
    p = ElasticParser()
    net = {"destination": {"ip": "8.8.8.8", "port": 53}, "network": {"transport": "udp"}}
    a = p.parse_event(make_doc("network", "connection_attempted", host_id="host-a", **net))
    b = p.parse_event(make_doc("network", "connection_attempted", host_id="host-b", **net))
    assert a.target is not b.target
    assert a.target.entity_id == "host-a|udp|8.8.8.8:53"


def test_fallback_key_includes_host_and_start_time():
    p = ElasticParser()
    proc1 = {"pid": 4412, "executable": "/usr/bin/bash", "start": "2026-11-01T10:00:00Z"}
    proc2 = {"pid": 4412, "executable": "/usr/bin/bash", "start": "2026-11-01T11:00:00Z"}
    a = p.parse_event(make_doc("file", "open", host_id="host-a", proc=proc1, file={"path": "/tmp/x"}))
    b = p.parse_event(make_doc("file", "open", host_id="host-b", proc=proc1, file={"path": "/tmp/x"}))
    c = p.parse_event(make_doc("file", "open", host_id="host-a", proc=proc2, file={"path": "/tmp/x"}))
    assert len({a.source.entity_id, b.source.entity_id, c.source.entity_id}) == 3
    assert a.source.attributes["id_source"] == "fallback"
    assert p.get_statistics()["process_ids_from_fallback"] == 3


def test_process_edge_points_parent_to_child():
    p = ElasticParser()
    proc = {"entity_id": "child-1", "pid": 200, "name": "nc", "executable": "/usr/bin/nc",
            "parent": {"entity_id": "parent-1", "pid": 100, "name": "bash"}}
    ev = p.parse_event(make_doc("process", "exec", proc=proc))
    assert ev.source.entity_id == "parent-1"
    assert ev.target.entity_id == "child-1"


def test_missing_parent_is_skipped_not_linked_to_placeholder():
    p = ElasticParser()
    proc = {"entity_id": "child-1", "pid": 200, "name": "nc"}
    assert p.parse_event(make_doc("process", "exec", proc=proc)) is None
    assert p.get_statistics()["events_missing_parent"] == 1
    assert not any(k.startswith("process:unknown") for k in p.entity_registry)


def test_process_without_any_id_is_skipped():
    p = ElasticParser()
    assert p.parse_event(make_doc("file", "open", proc={"name": "x"}, file={"path": "/a"})) is None
    assert p.get_statistics()["events_missing_process_id"] == 1


def test_file_without_path_is_skipped():
    p = ElasticParser()
    assert p.parse_event(make_doc("file", "open", file={})) is None


def test_real_event_id_is_kept():
    p = ElasticParser()
    ev = p.parse_event(make_doc("file", "open", event_id="evt-123", file={"path": "/a"}))
    assert ev.event_id == "evt-123"
    assert ev.attributes["raw_event_id"] == "evt-123"


def test_es_search_hit_is_unwrapped_and_id_kept():
    p = ElasticParser()
    hit = {"_index": ".ds-logs-endpoint.events.file-default", "_id": "es-abc",
           "_source": make_doc("file", "open", file={"path": "/a"})}
    assert p.can_parse(hit)
    ev = p.parse_event(hit)
    assert ev.event_id == "es-abc"
    assert ev.attributes["es_index"].startswith(".ds-logs-endpoint")


def test_synthetic_ids_are_unique():
    p = ElasticParser()
    doc = make_doc("file", "open", file={"path": "/a"})
    ids = {p.parse_event(doc).event_id for _ in range(3)}
    assert len(ids) == 3
    assert p.get_statistics()["events_synthetic_id"] == 3


def test_nanosecond_timestamp_parses():
    p = ElasticParser()
    ev = p.parse_event(make_doc("file", "open", ts="2026-11-01T12:00:00.123456789Z",
                                file={"path": "/a"}))
    expected = datetime(2026, 11, 1, 12, 0, 0, 123456, tzinfo=timezone.utc).timestamp()
    assert abs(ev.timestamp - expected) < 1e-6


def test_bad_timestamp_is_dropped():
    p = ElasticParser()
    assert p.parse_event(make_doc("file", "open", ts="not-a-time", file={"path": "/a"})) is None
    assert p.get_statistics()["events_bad_timestamp"] == 1


def test_category_list_picks_supported_category():
    p = ElasticParser()
    doc = make_doc("file", "open", file={"path": "/a"})
    doc["event"]["category"] = ["authentication", "file"]
    assert p.parse_event(doc).attributes["category"] == "file"


def test_end_to_end_ndjson_of_search_hits(tmp_path):
    hits = [
        {"_id": "1", "_source": make_doc("process", "exec", proc={
            "entity_id": "c1", "pid": 2, "name": "nc",
            "parent": {"entity_id": "p1", "pid": 1, "name": "bash"}})},
        {"_id": "2", "_source": make_doc("file", "open", proc={
            "entity_id": "c1", "pid": 2, "name": "nc"}, file={"path": "/etc/shadow"})},
    ]
    f = tmp_path / "endpoint.ndjson"
    f.write_text("\n".join(json.dumps(h) for h in hits))
    sp = SemanticParser()
    events = sp.parse_file(f)
    assert isinstance(sp.selected_parser, ElasticParser)
    assert [e.event_id for e in events] == ["1", "2"]
    # nc node is shared between its exec edge and its file edge
    assert events[0].target is events[1].source
