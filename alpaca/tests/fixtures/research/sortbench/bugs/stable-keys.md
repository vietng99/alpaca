# Bug: equal keys lose their input order

sortlib.sort(records, key=lambda r: r.group) returns records that share a group in a different
order than they came in. A stable sort keeps them in input order.
