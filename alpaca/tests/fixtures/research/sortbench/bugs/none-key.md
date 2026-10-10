# Bug: sort(items, key=None) raises

sortlib.sort(items, key=None) raises TypeError: 'NoneType' object is not callable. With no key the
sort should compare the items themselves.
