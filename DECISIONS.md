# DECISIONS.md 

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
```
I created four indexes:
   - `emp_code` on employees (unique): to guarantee that two employees can never share a code.
   - (`emp_code`, `date`) on attendance (unique): ensuring one record per employee per day. It also speeds up the monthly and trend queries.
   - (`date` descending, `emp_code` ascending) on attendance: used by the attendance list sort and by the month-wide queries (summary and leaderboard).
   - (`department`, `joined_on`) on employees: to quickly filter department's people quickly.

   I rejected an index on `status`. The other indexes already narrow down the result a lot, and every extra index makes write operation slower.
```

2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
```
If Two identical requests both find the employee and work out the same date. Both then try `insert_one`. The unique (`emp_code`, `date`) index lets MongoDB accept only the first one. 
The second gets `DuplicateKeyError`, and I return 409. I never check first and then insert later, so nothing can slip through between the two steps.
```

3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
```
We use standard ranking 1, 2, 2, 4. The `limit` is applied after ranking, so I return everyone whose rank is at or below the limit. 
Example: with `limit=2` and two people tied at rank 2, I return three rows. Dropping one of two equal people would be unfair.
```

4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
```
I count it with its own query on `employees` collection (for anyone joined on or before the month's last day), not from the logs. Then I merge it with the log totals by department. 
A person with no logs still counts and simply gets zeros. People who join later are not counted.
```

5. **One thing you would change** if this had to serve 100x the data.
```
Summary and leaderboard look up the employee for every single log. I would store `department` on each attendance record, so that lookup is not needed.
```