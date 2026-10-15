/* Keep one refresh in flight and suspend recurring work in hidden tabs. */
function startDashboardPolling(refresh, interval = 1000) {
    let timer = null;
    let running = false;
    let stopped = false;

    async function tick() {
        clearTimeout(timer);
        if (stopped || document.hidden || running) return;
        running = true;
        try {
            await refresh();
        } finally {
            running = false;
            if (!stopped && !document.hidden) timer = setTimeout(tick, interval);
        }
    }

    function visibilityChanged() {
        clearTimeout(timer);
        if (!document.hidden) void tick();
    }

    document.addEventListener('visibilitychange', visibilityChanged);
    if (!document.hidden) timer = setTimeout(tick, interval);
    return () => {
        stopped = true;
        clearTimeout(timer);
        document.removeEventListener('visibilitychange', visibilityChanged);
    };
}
