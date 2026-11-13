"""Background worker: expire unpaid orders, reconcile payments via alipay.trade.query, retry/poll fulfillments,
and (ONLY here) sync upstream costs + USD/CNY every 30 minutes.

`python -m app.worker` is the one independent worker process. Web processes (gunicorn / run.py) never run the
scheduled price sync; manual "sync now" from the admin uses the same DB lock so two syncs can never overlap."""
import sys
import threading
import time

from . import fulfillment, payments, pricing_service


def sweep_once(core):
    for fn in (payments.sweep, fulfillment.sweep):
        try:
            fn(core)
        except Exception as e:  # never kill the loop
            print("worker error in %s: %s" % (fn.__module__, type(e).__name__), file=sys.stderr)


def pricing_tick(core, force=False):
    """Run the 30-minute price sync if it is due (or force=True, used once at worker start)."""
    try:
        return pricing_service.run_cycle(core, force=force)
    except Exception as e:
        print("worker error in pricing sync: %s" % type(e).__name__, file=sys.stderr)
        return None


def start_thread(core, interval=10):
    """Dev convenience (run.py): order/fulfilment sweeping only - no price sync."""
    def loop():
        while True:
            sweep_once(core)
            time.sleep(interval)
    t = threading.Thread(target=loop, name="sweeper", daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    from .factory import build_core
    core = build_core()
    if "--once" in sys.argv:
        sweep_once(core)
        pricing_tick(core, force=True)
    else:
        pricing_tick(core, force=True)          # once at start-up, then every 30 minutes (next_sync_at)
        while True:
            sweep_once(core)
            pricing_tick(core)
            time.sleep(10)
