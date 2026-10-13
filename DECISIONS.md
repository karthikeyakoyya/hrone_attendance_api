# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?

Five. `employees.emp_code` (unique): lookups, duplicate protection. `employees (department, joined_on)`: department filter and the trend. `attendance_logs (emp_code, date)` (unique): one record per day, per-employee queries. `(date desc, emp_code, late_minutes)`: list order, month scans, and the leaderboard straight from the index. `(status, date desc, emp_code)`: status filters and their counts. Rejected `(emp_code, punch_in)`: the first logs index already narrows punch-out to one employee.

2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.

Both requests call `insert_one`. The unique index on `(emp_code, date)` lets one through, the other gets a duplicate key error and returns 409. I do not look the record up first, so there is no gap between a check and a write.

3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?

`$rank` gives tied employees the same rank, and I keep `rank <= limit` after ranking. Everyone tied at the cutoff is returned, so the list can be longer than `limit`.

4. **Headcount.** How does your department summary make sure employees with zero logs are counted?

Logs are summed per employee, then the employees are unioned in and merged on emp_code. Employees with no logs come from the union with zero totals, so they count. Late joiners are not in the union.

5. **One thing you would change** if this had to serve 100x the data.

Keep monthly totals per employee in a summary collection, updated on each punch and correction, so analytics read small documents instead of scanning logs.
