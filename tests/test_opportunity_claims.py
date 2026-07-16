import sqlite3
import threading
import time
import pytest
from core.database import Database

def test_concurrent_opportunity_claims(tmp_path):
    db_path = str(tmp_path / "test.db")
    db = Database(db_path)
    
    # Insert an opportunity
    cursor = db.get_cursor()
    cursor.execute("""
        INSERT INTO opportunities 
        (target_id, platform, project, title, url, status) 
        VALUES ('123', 'reddit', 'proj1', 'Test', 'url', 'pending')
    """)
    opp_id = cursor.lastrowid
    db._conn.commit()

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
    opp = db.get_opportunity('123')
    assert opp['status'] == 'claimed'
    
    db.close()

if __name__ == '__main__':
    pytest.main(['-q', __file__])
