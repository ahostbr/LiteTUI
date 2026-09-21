import multiprocessing as mp
import time


def _reserve(path, barrier, queue, amount=60):
    from litetui.resource_admission import ResourceCoordinator, ResourceSnapshot, ModelDemand
    coordinator = ResourceCoordinator(path, telemetry=lambda: ResourceSnapshot(time.time(), 100, {'gpu': 100}, True))
    barrier.wait(10)
    result = coordinator.reserve(ModelDemand('local', 'endpoint', 'model', amount, {'gpu': amount}), str(mp.current_process().pid))
    queue.put(result.status)


def test_two_processes_only_one_reservation(tmp_path):
    context = mp.get_context('spawn')
    barrier, queue = context.Barrier(2), context.Queue()
    workers = [context.Process(target=_reserve, args=(str(tmp_path / 'shared.sqlite'), barrier, queue)) for _ in range(2)]
    for worker in workers: worker.start()
    try:
        assert sorted(queue.get(timeout=20) for _ in workers) == ['admitted', 'blocked']
        for worker in workers:
            worker.join(10)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive(): worker.terminate(); worker.join()
        queue.close()


def test_two_processes_cannot_dispatch_same_model_even_with_spare_capacity(tmp_path):
    context = mp.get_context('spawn')
    barrier, queue = context.Barrier(2), context.Queue()
    workers = [context.Process(target=_reserve, args=(str(tmp_path / 'ample.sqlite'), barrier, queue, 10)) for _ in range(2)]
    for worker in workers:
        worker.start()
    try:
        assert sorted(queue.get(timeout=20) for _ in workers) == ['admitted', 'blocked']
        for worker in workers:
            worker.join(10)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join()
        queue.close()
