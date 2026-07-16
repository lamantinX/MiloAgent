import pytest
from core.job_coordinator import JobCoordinator, AlreadyRunning

def test_job_coordinator_exclusive_per_business():
    coordinator = JobCoordinator()
    
    # Can start job for business_A
    ticket1 = coordinator.start_job("ACT", "bus_A")
    assert not ticket1.is_cancelled
    
    # Cannot start same job for same business
    with pytest.raises(AlreadyRunning):
        coordinator.start_job("ACT", "bus_A")
        
    # Can start same job for different business
    ticket2 = coordinator.start_job("ACT", "bus_B")
    
    # Can start different job for same business
    ticket3 = coordinator.start_job("SCAN", "bus_A")
    
    # Finish job
    coordinator.finish_job(ticket1)
    
    # Now can start again
    ticket4 = coordinator.start_job("ACT", "bus_A")
    
if __name__ == '__main__':
    pytest.main(['-q', __file__])
