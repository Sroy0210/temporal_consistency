import sqlite3

with sqlite3.connect("data/db/linux.db") as conn:
    q = lambda sql: conn.execute(sql).fetchone()
    print("commits:", q("SELECT COUNT(*) FROM commits")[0])
    print("shallow-boundary (false hits):", q("SELECT SUM(is_shallow_boundary) FROM commits")[0])
    print("real ext4 commits:", q("""
        SELECT COUNT(DISTINCT commit_hash) FROM file_changes WHERE in_scope = 1""")[0])
    print("with Reviewed-by / Link / Fixes:", q("""
        SELECT SUM(EXISTS(SELECT 1 FROM trailers t WHERE t.commit_hash=c.commit_hash AND kind='reviewed_by')),
               SUM(EXISTS(SELECT 1 FROM trailers t WHERE t.commit_hash=c.commit_hash AND kind='link')),
               SUM(EXISTS(SELECT 1 FROM trailers t WHERE t.commit_hash=c.commit_hash AND kind='fixes'))
        FROM commits c WHERE is_shallow_boundary = 0"""))
    print("Fixes: tags / resolved to a commit here:", q(
        "SELECT COUNT(*), SUM(fixed_commit IS NOT NULL) FROM fix_links"))
    print("reverts:", q("SELECT SUM(is_revert) FROM commits")[0])
    print("Timing window:", q("SELECT MIN(committer_timestamp), MAX(committer_timestamp) " \
            "FROM commits WHERE is_shallow_boundary = 0"))
    print("ext4 commits on shallow boundary:", q("SELECT COUNT(*) FROM commits WHERE is_shallow_boundary = 1 AND subject LIKE 'ext4:%'"))