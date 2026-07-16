import sqlite3
import threading
import time
import pytest
from core.database import Database

def test_concurrent_opportunity_claims(tmp_path):
    db_path = str(tmp_path / "test.db")
    db = Database(db_path)
    
    # Insert an opportunity
    cursor = db.conn.cursor()
    cursor.execute("""
        INSERT INTO opportunities 
        (target_id, platform, business_id, project, title, subreddit_or_query, status) 
        VALUES ('123', 'reddit', 'biz_test', 'proj1', 'Test', 'url', 'pending')
    """)
    opp_id = cursor.lastrowid
    db.conn.commit()

    # The goal: Multiple threads try to claim the exact same ID.
    # Only one should get True, the rest False.
    results = []
    
    def claim_worker():
        # Open a new connection/database object to simulate threading properly
        # actually core.database.Database handles its own threading inside
        res = db.claim_opportunity(opp_id)
        results.append(res)
        
    threads = [threading.Thread(target=claim_worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
        
    assert results.count(True) == 1
    assert results.count(False) == 9
    
    # Check status
    opp = db.conn.execute("SELECT * FROM opportunities WHERE target_id = '123'").fetchone()
    assert opp["status"] == 'claimed'
    
    db.close()

if __name__ == '__main__':
    pytest.main(['-q', __file__])
