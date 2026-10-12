-- Remittance document embeddings — SQLite dialect of
-- migrations/0002_document_embeddings.sql. PostgreSQL stores the
-- vector in a pgvector column and can lean on its index; SQLite has
-- no pgvector, so the vector is stored as JSON text and similarity
-- is computed in code (cash_recon/documents.py) over the stored
-- vectors — at a desk's document volume that is a handful of
-- arithmetic, and the results are identical: same embedder, same
-- cosine, same threshold. Only the machinery differs.
CREATE TABLE IF NOT EXISTS remittance_embeddings (
    remittance_id TEXT PRIMARY KEY,
    embedding     TEXT NOT NULL
);
