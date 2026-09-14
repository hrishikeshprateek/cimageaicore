import math

from services.ai_gateway.embeddings import MockEmbedder


def test_mock_embedder_is_deterministic_normalised_and_similarity_aware():
    e = MockEmbedder(dimensions=256)
    a, b = e.embed_documents([("t", "students discussed placements and careers"), ("t", "robotics lab with humanoid robots")])
    assert len(a) == 256 and abs(math.sqrt(sum(x * x for x in a)) - 1) < 1e-6
    assert a == e.embed_documents([("t", "students discussed placements and careers")])[0]
    q = e.embed_query("placements careers students")
    dot = lambda u, v: sum(x * y for x, y in zip(u, v))
    assert dot(q, a) > dot(q, b)
