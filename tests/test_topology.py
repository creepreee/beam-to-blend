from importer.topology import TopologyHasher


def test_topology_hash_changes_when_edge_order_changes():
    a = TopologyHasher.hash_connectivity(
        3,
        edges=[(0, 1), (1, 2), (2, 0)],
        faces=[(0, 1, 2)],
    )
    b = TopologyHasher.hash_connectivity(
        3,
        edges=[(1, 0), (1, 2), (2, 0)],
        faces=[(0, 1, 2)],
    )
    assert a != b
