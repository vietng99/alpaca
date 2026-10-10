"""Per-document capture work must not walk every active block in the wiki."""
from alpaca.wiki.config import Config
from alpaca.wiki.store.db import DB


def test_capture_queries_use_document_and_status_index_after_schema_pour(tmp_path):
    database = DB(Config.for_vault(tmp_path))
    try:
        database.pour()
        queries = [
            "SELECT block_id FROM blocks WHERE doc_id=? AND status='active'",
            "SELECT e.edge_id FROM edges e JOIN blocks b ON e.source_block_id=b.block_id "
            "WHERE b.doc_id=? AND b.status='active'",
        ]
        def check():
            for query in queries:
                plans = database.conn.execute('EXPLAIN QUERY PLAN '+query, ('raw/session.md',)).fetchall()
                assert any('doc_id=? AND status=?' in row['detail'] for row in plans), plans
        check()
        # Existing stores get the same additive index on their next normal pour.
        database.conn.execute('DROP INDEX idx_blocks_doc_status')
        database.conn.commit()
        database.pour()
        check()
    finally:
        database.close()
