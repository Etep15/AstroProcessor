import sys

class ProgressReporter:
    """
    Handles real-time progress reporting for AstroProcessor operations.
    Uses periods to indicate progress and flushes output immediately.
    """
    def __init__(self, label, total):
        self.label = label
        self.total = total
        self.completed = 0
        self.stats = {}
        # Print the initial progress prefix
        print(f"{self.label} {self.total}", end="", flush=True)

    def increment(self, stat_key=None):
        """Record one completed item and print a progress period."""
        self.completed += 1
        if stat_key:
            self.stats[stat_key] = self.stats.get(stat_key, 0) + 1
        print(".", end="", flush=True)

    def finish(self, result_text=""):
        """End the progress line and print completion result."""
        result = " done"
        if result_text:
            result += f" ({result_text})"
        elif self.stats:
            # Auto-generate stats text if available
            stats_parts = [f"{k}: {v}" for k, v in self.stats.items()]
            result += f" ({', '.join(stats_parts)})"
        print(f"{result}", flush=True)

    def fail(self, error_msg):
        """End the progress line and report a fatal failure."""
        print(f"\nFAILED after {self.completed} of {self.total} files: {error_msg}", flush=True)
