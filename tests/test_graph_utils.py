from rdflib import OWL, RDF, RDFS, Graph, Namespace

from domain.tbox_utils import _new_graph

EX = Namespace("http://example.org/")


def _make_graph(n_triples: int) -> Graph:
    g = _new_graph()
    for i in range(n_triples):
        g.add((EX[f"inst_{i}"], RDF.type, EX.TestClass))
    return g


class TestPaginatedQuery:
    def test_returns_all_results(self):
        g = _make_graph(25)
        from domain.graph_utils import paginated_query
        results = list(paginated_query(g, "SELECT ?s WHERE { ?s a <http://example.org/TestClass> }", batch_size=10))
        assert len(results) == 25

    def test_small_graph_single_batch(self):
        g = _make_graph(3)
        from domain.graph_utils import paginated_query
        results = list(paginated_query(g, "SELECT ?s WHERE { ?s a <http://example.org/TestClass> }", batch_size=100))
        assert len(results) == 3

    def test_empty_graph(self):
        g = _new_graph()
        from domain.graph_utils import paginated_query
        results = list(paginated_query(g, "SELECT ?s WHERE { ?s a <http://example.org/TestClass> }", batch_size=10))
        assert len(results) == 0

    def test_exact_batch_boundary(self):
        g = _make_graph(20)
        from domain.graph_utils import paginated_query
        results = list(paginated_query(g, "SELECT ?s WHERE { ?s a <http://example.org/TestClass> }", batch_size=10))
        assert len(results) == 20


class TestBuildClassAdjacency:
    def _make_tbox_with_ops(self) -> Graph:
        g = _new_graph()
        g.add((EX.hasEquipment, RDF.type, OWL.ObjectProperty))
        g.add((EX.hasEquipment, RDFS.domain, EX.Process))
        g.add((EX.hasEquipment, RDFS.range, EX.Equipment))
        g.add((EX.hasSensor, RDF.type, OWL.ObjectProperty))
        g.add((EX.hasSensor, RDFS.domain, EX.Equipment))
        g.add((EX.hasSensor, RDFS.range, EX.Sensor))
        g.add((EX.isEquipmentOf, RDF.type, OWL.ObjectProperty))
        g.add((EX.isEquipmentOf, OWL.inverseOf, EX.hasEquipment))
        g.add((EX.isEquipmentOf, RDFS.domain, EX.Equipment))
        g.add((EX.isEquipmentOf, RDFS.range, EX.Process))
        return g

    def test_adjacency_includes_forward(self):
        from domain.graph_utils import build_class_adjacency
        tbox = self._make_tbox_with_ops()
        adj = build_class_adjacency(tbox)
        neighbors = {cls for cls, _ in adj[EX.Process]}
        assert EX.Equipment in neighbors

    def test_adjacency_includes_inverse(self):
        from domain.graph_utils import build_class_adjacency
        tbox = self._make_tbox_with_ops()
        adj = build_class_adjacency(tbox)
        neighbors = {cls for cls, _ in adj[EX.Equipment]}
        assert EX.Process in neighbors

    def test_multi_hop_path(self):
        from domain.graph_utils import build_class_adjacency, find_path_bfs
        tbox = self._make_tbox_with_ops()
        adj = build_class_adjacency(tbox)
        path = find_path_bfs(adj, EX.Process, EX.Sensor, max_hops=3)
        assert path is not None
        assert path["hops"] == 2
        assert EX.Equipment in path["path"]

    def test_no_path_returns_none(self):
        from domain.graph_utils import build_class_adjacency, find_path_bfs
        tbox = self._make_tbox_with_ops()
        tbox.add((EX.Isolated, RDF.type, OWL.Class))
        adj = build_class_adjacency(tbox)
        path = find_path_bfs(adj, EX.Process, EX.Isolated, max_hops=3)
        assert path is None

    def test_max_hops_limit(self):
        from domain.graph_utils import build_class_adjacency, find_path_bfs
        tbox = self._make_tbox_with_ops()
        adj = build_class_adjacency(tbox)
        path = find_path_bfs(adj, EX.Process, EX.Sensor, max_hops=1)
        assert path is None
