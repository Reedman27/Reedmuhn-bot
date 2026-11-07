"""Connection wrapper used to make multi-step config restores atomic."""

class _DeferredCommitConn:
    """Wraps a sqlite3.Connection so commit() is a no-op while everything
    else (execute, rollback, ...) passes straight through.

    Used only by Db.restore_config_snapshot_data to make a multi-step restore
    atomic even though it's built from public setter methods that each call
    self.conn.commit() individually. A sqlite3.Connection's own methods can't
    be reassigned directly (attribute assignment on the C-level object raises
    AttributeError), so this stands in for the whole connection instead -
    self.conn is swapped to point at one of these for the duration, then
    swapped back, and it's the swapped-back real connection's commit()/
    rollback() that actually ends the transaction.
    """

    def __init__(self, real_conn):
        self._real = real_conn

    def commit(self):
        pass

    def __getattr__(self, name):
        return getattr(self._real, name)
