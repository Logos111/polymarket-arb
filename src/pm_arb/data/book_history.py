from pm_arb.data.series_buffer import SeriesBuffer


class BookHistory:
    # Rolling buffers of both sides best ask/bid (4 SeriesBuffer).
    # Live: orchestrator update(elapsed, book) per poll; backtest: engine
    # capture per tick -- same class, two call sites, no logic fork.
    # Underdog chosen dynamically; series kept per side (Up/Down).
    SIDES = ('Up', 'Down')

    def __init__(self, maxlen_sec=180.0):
        self.ask = {s: SeriesBuffer(maxlen_sec) for s in self.SIDES}
        self.bid = {s: SeriesBuffer(maxlen_sec) for s in self.SIDES}

    def update(self, t, book):
        for s in self.SIDES:
            v = book.get(s) or {}
            self.ask[s].push(t, v.get('best_ask'))
            self.bid[s].push(t, v.get('best_bid'))

    def bufs(self, side):
        # Return (ask_buf, bid_buf) for compute_features.
        return self.ask[side], self.bid[side]
