# Review profile: local-pair

Read only when selected by the frozen run contract. Step numbers refer to contract section 6.

**local-pair.** One PR per task, or at most four independent tasks; a task moving a committed
  generated identity lands alone. Step 1 runs the pair on `origin/main..head` before each push; after it run
  `LOOP.md`'s `review-status:` and `gate-status:`, if named, on the head. Steps 3-5 become: pair
  passed and every leg green, run `merge-hold:`, if named, on main's tip and the PR; exit 0 enables
  `gh pr merge <n> --auto --squash --match-head-commit <sha>` with step 7's subject and
  body, else retry when main moves.
