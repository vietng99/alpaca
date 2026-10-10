# Lessons of the sortbench pack

- A merge sort with a cut-over to insertion sort below 16 items compares less than a plain merge
  sort on the fixed inputs.
- Calling the key function once per item, before the sort, is cheaper than calling it per
  comparison.
