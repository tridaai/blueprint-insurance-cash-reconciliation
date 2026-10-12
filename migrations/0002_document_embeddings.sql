-- Remittance document embeddings (pgvector). The pipeline stores one
-- embedding per remittance document so a near-identical advice arriving
-- twice — the classic duplicate remittance — is caught even when the
-- sender re-words the covering note. The offline embedder is a
-- deterministic hashing embedder (see cash_recon/documents.py); a
-- configured model embedder can replace it behind the same port.
CREATE TABLE IF NOT EXISTS remittance_embeddings (
    remittance_id TEXT PRIMARY KEY,
    embedding     vector(384) NOT NULL
);
