"""Long options git accepts for the subcommands whose options the attach guard resolves (op-062 t-651).

git takes any unique prefix of a long option (`--no-veri` is `--no-verify`). The guard resolves each
`--word` against these lists; a word that is no option here, or the prefix of more than one, is a call
the guard cannot read. Generated with git version 2.53.0 from `git <sub> --git-completion-helper-all` joined with the
long options `git <sub> -h` prints (archive lists its archiver options only there). Regenerate when git
adds options; an option missing here is denied, not allowed."""

LONG = {
    "add": frozenset("""
        --all --chmod --dry-run --edit --force --ignore-errors --ignore-missing --ignore-removal --intent-to-add
        --inter-hunk-context --interactive --no-all --no-chmod --no-dry-run --no-edit --no-force --no-ignore-errors
        --no-ignore-missing --no-ignore-removal --no-intent-to-add --no-interactive --no-patch
        --no-pathspec-file-nul --no-pathspec-from-file --no-refresh --no-renormalize --no-sparse --no-update
        --no-verbose --no-warn-embedded-repo --patch --pathspec-file-nul --pathspec-from-file --refresh
        --renormalize --sparse --unified --update --verbose --warn-embedded-repo
        """.split()),
    "am": frozenset("""
        --3way --abort --allow-empty --binary --committer-date-is-author-date --continue --directory --empty
        --exclude --gpg-sign --ignore-date --ignore-space-change --ignore-whitespace --include --interactive --keep
        --keep-cr --keep-non-patch --message-id --no-3way --no-binary --no-committer-date-is-author-date
        --no-directory --no-exclude --no-gpg-sign --no-ignore-date --no-ignore-space-change --no-ignore-whitespace
        --no-include --no-interactive --no-keep --no-keep-cr --no-keep-non-patch --no-message-id --no-patch-format
        --no-quiet --no-rebasing --no-reject --no-rerere-autoupdate --no-resolvemsg --no-scissors --no-signoff
        --no-utf8 --no-verify --no-whitespace --patch-format --quiet --quit --quoted-cr --rebasing --reject
        --rerere-autoupdate --resolved --resolvemsg --retry --scissors --show-current-patch --signoff --skip --utf8
        --verify --whitespace
        """.split()),
    "apply": frozenset("""
        --3way --add --allow-binary-replacement --allow-empty --allow-overlap --apply --binary --build-fake-ancestor
        --cached --check --directory --exclude --ignore-space-change --ignore-whitespace --inaccurate-eof --include
        --index --intent-to-add --no-3way --no-add --no-allow-binary-replacement --no-allow-empty --no-allow-overlap
        --no-apply --no-binary --no-build-fake-ancestor --no-cached --no-check --no-directory
        --no-ignore-space-change --no-ignore-whitespace --no-inaccurate-eof --no-index --no-intent-to-add
        --no-numstat --no-quiet --no-recount --no-reject --no-reverse --no-stat --no-summary --no-unidiff-zero
        --no-unsafe-paths --no-verbose --no-whitespace --numstat --ours --quiet --recount --reject --reverse --stat
        --summary --theirs --unidiff-zero --union --unsafe-paths --verbose --whitespace
        """.split()),
    "archive": frozenset("""
        --add-file --add-virtual-file --exec --format --list --mtime --no-add-file --no-add-virtual-file --no-exec
        --no-format --no-list --no-output --no-prefix --no-remote --no-verbose --no-worktree-attributes --output
        --prefix --remote --verbose --worktree-attributes
        """.split()),
    "branch": frozenset("""
        --abbrev --all --color --column --contains --copy --create-reflog --delete --edit-description --force
        --format --ignore-case --list --merged --move --no-abbrev --no-color --no-column --no-contains --no-copy
        --no-create-reflog --no-delete --no-edit-description --no-force --no-format --no-ignore-case --no-list
        --no-merged --no-move --no-omit-empty --no-points-at --no-quiet --no-recurse-submodules --no-set-upstream
        --no-set-upstream-to --no-show-current --no-sort --no-track --no-unset-upstream --no-verbose --omit-empty
        --points-at --quiet --recurse-submodules --remotes --set-upstream --set-upstream-to --show-current --sort
        --track --unset-upstream --verbose --with --without
        """.split()),
    "checkout": frozenset("""
        --conflict --detach --force --guess --ignore-other-worktrees --ignore-skip-worktree-bits
        --inter-hunk-context --merge --no-conflict --no-detach --no-force --no-guess --no-ignore-other-worktrees
        --no-ignore-skip-worktree-bits --no-merge --no-orphan --no-overlay --no-overwrite-ignore --no-patch
        --no-pathspec-file-nul --no-pathspec-from-file --no-progress --no-quiet --no-recurse-submodules --no-track
        --orphan --ours --overlay --overwrite-ignore --patch --pathspec-file-nul --pathspec-from-file --progress
        --quiet --recurse-submodules --theirs --track --unified
        """.split()),
    "checkout-index": frozenset("""
        --all --create --force --ignore-skip-worktree-bits --index --no-all --no-create --no-force
        --no-ignore-skip-worktree-bits --no-index --no-prefix --no-quiet --no-stdin --no-temp --prefix --quiet
        --stage --stdin --temp
        """.split()),
    "cherry-pick": frozenset("""
        --abort --allow-empty --allow-empty-message --cleanup --commit --continue --edit --empty --ff --gpg-sign
        --keep-redundant-commits --mainline --no-allow-empty --no-allow-empty-message --no-cleanup --no-commit
        --no-edit --no-ff --no-gpg-sign --no-keep-redundant-commits --no-mainline --no-rerere-autoupdate
        --no-signoff --no-strategy --no-strategy-option --quit --rerere-autoupdate --signoff --skip --strategy
        --strategy-option
        """.split()),
    "clean": frozenset("""
        --dry-run --exclude --force --interactive --no-dry-run --no-force --no-interactive --no-quiet --quiet
        """.split()),
    "clone": frozenset("""
        --also-filter-submodules --bare --branch --bundle-uri --checkout --config --depth --dissociate --filter
        --hardlinks --ipv4 --ipv6 --jobs --local --mirror --naked --no-also-filter-submodules --no-bare --no-branch
        --no-bundle-uri --no-checkout --no-config --no-depth --no-dissociate --no-filter --no-hardlinks --no-jobs
        --no-local --no-mirror --no-naked --no-origin --no-progress --no-quiet --no-recurse-submodules
        --no-recursive --no-ref-format --no-reference --no-reference-if-able --no-reject-shallow
        --no-remote-submodules --no-revision --no-separate-git-dir --no-server-option --no-shallow-exclude
        --no-shallow-since --no-shallow-submodules --no-shared --no-single-branch --no-sparse --no-tags
        --no-template --no-upload-pack --no-verbose --origin --progress --quiet --recurse-submodules --recursive
        --ref-format --reference --reference-if-able --reject-shallow --remote-submodules --revision
        --separate-git-dir --server-option --shallow-exclude --shallow-since --shallow-submodules --shared
        --single-branch --sparse --tags --template --upload-pack --verbose
        """.split()),
    "commit": frozenset("""
        --ahead-behind --all --allow-empty --allow-empty-message --amend --author --branch --cleanup --date
        --dry-run --edit --file --fixup --gpg-sign --include --inter-hunk-context --interactive --long --message
        --no-ahead-behind --no-all --no-allow-empty --no-allow-empty-message --no-amend --no-author --no-branch
        --no-cleanup --no-date --no-dry-run --no-edit --no-file --no-fixup --no-gpg-sign --no-include
        --no-interactive --no-long --no-message --no-null --no-only --no-patch --no-pathspec-file-nul
        --no-pathspec-from-file --no-porcelain --no-post-rewrite --no-quiet --no-reedit-message --no-reset-author
        --no-reuse-message --no-short --no-signoff --no-squash --no-status --no-template --no-untracked-files
        --no-verbose --no-verify --null --only --patch --pathspec-file-nul --pathspec-from-file --porcelain
        --post-rewrite --quiet --reedit-message --reset-author --reuse-message --short --signoff --squash --status
        --template --trailer --unified --untracked-files --verbose --verify
        """.split()),
    "fetch": frozenset("""
        --all --append --atomic --auto --auto-gc --auto-maintenance --deepen --depth --dry-run --filter --force
        --ipv4 --ipv6 --jobs --keep --multiple --negotiate-only --negotiation-tip --no-all --no-append --no-atomic
        --no-auto-gc --no-auto-maintenance --no-deepen --no-depth --no-dry-run --no-filter --no-force --no-jobs
        --no-keep --no-multiple --no-negotiate-only --no-negotiation-tip --no-porcelain --no-prefetch --no-progress
        --no-prune --no-prune-tags --no-quiet --no-recurse-submodules --no-recurse-submodules-default
        --no-server-option --no-set-upstream --no-shallow-exclude --no-shallow-since --no-show-forced-updates
        --no-stdin --no-submodule-prefix --no-tags --no-update-head-ok --no-update-shallow --no-upload-pack
        --no-verbose --no-write-commit-graph --no-write-fetch-head --porcelain --prefetch --progress --prune
        --prune-tags --quiet --recurse-submodules --recurse-submodules-default --refetch --refmap --server-option
        --set-upstream --shallow-exclude --shallow-since --show-forced-updates --stdin --submodule-prefix --tags
        --unshallow --update-head-ok --update-shallow --upload-pack --verbose --write-commit-graph
        --write-fetch-head
        """.split()),
    "format-patch": frozenset("""
        --add-header --attach --base --binary --cc --cover-from-description --cover-letter --creation-factor
        --description-file --filename-max-length --force-in-body-from --from --ignore-if-in-upstream --in-reply-to
        --inline --interdiff --keep-subject --no-add-header --no-attach --no-base --no-binary --no-cc
        --no-cover-from-description --no-cover-letter --no-creation-factor --no-description-file
        --no-filename-max-length --no-force-in-body-from --no-from --no-ignore-if-in-upstream --no-in-reply-to
        --no-interdiff --no-numbered --no-numbered-files --no-progress --no-quiet --no-range-diff --no-reroll-count
        --no-rfc --no-signature --no-signature-file --no-signoff --no-start-number --no-stat --no-stdout --no-suffix
        --no-thread --no-to --no-zero-commit --numbered --numbered-files --output-directory --progress --quiet
        --range-diff --reroll-count --rfc --signature --signature-file --signoff --start-number --stdout
        --subject-prefix --suffix --thread --to --zero-commit
        """.split()),
    "ls-remote": frozenset("""
        --branches --exec --exit-code --get-url --heads --no-branches --no-exec --no-exit-code --no-get-url
        --no-heads --no-quiet --no-refs --no-server-option --no-sort --no-symref --no-tags --no-upload-pack --quiet
        --refs --server-option --sort --symref --tags --upload-pack
        """.split()),
    "merge": frozenset("""
        --abort --allow-unrelated-histories --autostash --cleanup --commit --compact-summary --continue --edit --ff
        --ff-only --file --gpg-sign --into-name --log --message --no-abort --no-allow-unrelated-histories
        --no-autostash --no-cleanup --no-commit --no-compact-summary --no-continue --no-edit --no-ff --no-gpg-sign
        --no-into-name --no-log --no-message --no-overwrite-ignore --no-progress --no-quiet --no-quit
        --no-rerere-autoupdate --no-signoff --no-squash --no-stat --no-strategy --no-strategy-option --no-summary
        --no-verbose --no-verify --no-verify-signatures --overwrite-ignore --progress --quiet --quit
        --rerere-autoupdate --signoff --squash --stat --strategy --strategy-option --summary --verbose --verify
        --verify-signatures
        """.split()),
    "mv": frozenset("""
        --dry-run --force --no-dry-run --no-force --no-sparse --no-verbose --sparse --verbose
        """.split()),
    "pull": frozenset("""
        --all --allow-unrelated-histories --append --autostash --cleanup --commit --compact-summary --deepen --depth
        --dry-run --edit --ff --ff-only --force --gpg-sign --ipv4 --ipv6 --jobs --keep --log --negotiation-tip
        --no-all --no-allow-unrelated-histories --no-append --no-autostash --no-cleanup --no-commit
        --no-compact-summary --no-deepen --no-depth --no-dry-run --no-edit --no-ff --no-force --no-gpg-sign
        --no-ipv4 --no-ipv6 --no-jobs --no-keep --no-log --no-negotiation-tip --no-progress --no-prune --no-quiet
        --no-rebase --no-recurse-submodules --no-server-option --no-set-upstream --no-shallow-exclude
        --no-shallow-since --no-show-forced-updates --no-signoff --no-squash --no-stat --no-strategy
        --no-strategy-option --no-summary --no-tags --no-update-shallow --no-upload-pack --no-verbose --no-verify
        --no-verify-signatures --progress --prune --quiet --rebase --recurse-submodules --refmap --server-option
        --set-upstream --shallow-exclude --shallow-since --show-forced-updates --signoff --squash --stat --strategy
        --strategy-option --summary --tags --unshallow --update-shallow --upload-pack --verbose --verify
        --verify-signatures
        """.split()),
    "read-tree": frozenset("""
        --aggressive --debug-unpack --dry-run --empty --exclude-per-directory --index-output --no-aggressive
        --no-debug-unpack --no-dry-run --no-empty --no-quiet --no-recurse-submodules --no-reset --no-sparse-checkout
        --no-super-prefix --no-trivial --no-verbose --prefix --quiet --recurse-submodules --reset --sparse-checkout
        --super-prefix --trivial --verbose
        """.split()),
    "rebase": frozenset("""
        --abort --allow-empty-message --apply --autosquash --autostash --committer-date-is-author-date --continue
        --edit-todo --empty --exec --ff --force-rebase --fork-point --gpg-sign --ignore-date --ignore-whitespace
        --interactive --keep-base --keep-empty --merge --no-allow-empty-message --no-autosquash --no-autostash
        --no-committer-date-is-author-date --no-exec --no-ff --no-force-rebase --no-fork-point --no-gpg-sign
        --no-ignore-date --no-ignore-whitespace --no-keep-base --no-keep-empty --no-onto --no-preserve-merges
        --no-quiet --no-reapply-cherry-picks --no-rebase-merges --no-rerere-autoupdate --no-reschedule-failed-exec
        --no-reset-author-date --no-root --no-signoff --no-stat --no-strategy --no-strategy-option --no-update-refs
        --no-verbose --no-verify --no-whitespace --onto --preserve-merges --quiet --quit --reapply-cherry-picks
        --rebase-merges --rerere-autoupdate --reschedule-failed-exec --reset-author-date --root --show-current-patch
        --signoff --skip --stat --strategy --strategy-option --update-refs --verbose --verify --whitespace
        """.split()),
    "reset": frozenset("""
        --hard --intent-to-add --inter-hunk-context --keep --merge --mixed --no-intent-to-add --no-patch
        --no-pathspec-file-nul --no-pathspec-from-file --no-quiet --no-recurse-submodules --no-refresh --patch
        --pathspec-file-nul --pathspec-from-file --quiet --recurse-submodules --refresh --soft --unified
        """.split()),
    "restore": frozenset("""
        --conflict --ignore-skip-worktree-bits --ignore-unmerged --inter-hunk-context --merge --no-conflict
        --no-ignore-skip-worktree-bits --no-ignore-unmerged --no-merge --no-overlay --no-patch
        --no-pathspec-file-nul --no-pathspec-from-file --no-progress --no-quiet --no-recurse-submodules --no-source
        --no-staged --no-worktree --ours --overlay --patch --pathspec-file-nul --pathspec-from-file --progress
        --quiet --recurse-submodules --source --staged --theirs --unified --worktree
        """.split()),
    "revert": frozenset("""
        --abort --cleanup --commit --continue --edit --gpg-sign --mainline --no-cleanup --no-commit --no-edit
        --no-gpg-sign --no-mainline --no-reference --no-rerere-autoupdate --no-signoff --no-strategy
        --no-strategy-option --quit --reference --rerere-autoupdate --signoff --skip --strategy --strategy-option
        """.split()),
    "rm": frozenset("""
        --cached --dry-run --force --ignore-unmatch --no-cached --no-dry-run --no-force --no-ignore-unmatch
        --no-pathspec-file-nul --no-pathspec-from-file --no-quiet --no-sparse --pathspec-file-nul
        --pathspec-from-file --quiet --sparse
        """.split()),
    "tag": frozenset("""
        --annotate --cleanup --color --column --contains --create-reflog --delete --edit --file --force --format
        --ignore-case --list --local-user --merged --message --no-annotate --no-cleanup --no-color --no-column
        --no-contains --no-create-reflog --no-edit --no-file --no-force --no-format --no-ignore-case --no-local-user
        --no-merged --no-omit-empty --no-points-at --no-sign --no-sort --omit-empty --points-at --sign --sort
        --trailer --verify --with --without
        """.split()),
    "update-index": frozenset("""
        --add --again --assume-unchanged --cacheinfo --chmod --clear-resolve-undo --force-remove
        --force-untracked-cache --force-write-index --fsmonitor --fsmonitor-valid --ignore-missing
        --ignore-skip-worktree-entries --ignore-submodules --index-info --index-version --info-only --no-add
        --no-assume-unchanged --no-force-remove --no-force-untracked-cache --no-force-write-index --no-fsmonitor
        --no-fsmonitor-valid --no-ignore-missing --no-ignore-skip-worktree-entries --no-ignore-submodules
        --no-index-version --no-info-only --no-remove --no-replace --no-show-index-version --no-skip-worktree
        --no-split-index --no-test-untracked-cache --no-unmerged --no-untracked-cache --no-verbose --really-refresh
        --refresh --remove --replace --show-index-version --skip-worktree --split-index --stdin
        --test-untracked-cache --unmerged --unresolve --untracked-cache --verbose
        """.split()),
}

#: long options that take their value in the next word when no `=` is given
TAKES_VALUE = {
    "add": frozenset("""
        --chmod --inter-hunk-context --pathspec-from-file --unified
        """.split()),
    "am": frozenset("""
        --directory --empty --exclude --include --patch-format --quoted-cr --resolvemsg --whitespace
        """.split()),
    "apply": frozenset("""
        --build-fake-ancestor --directory --exclude --include --whitespace
        """.split()),
    "archive": frozenset("""
        --add-file --add-virtual-file --exec --format --mtime --output --prefix --remote
        """.split()),
    "branch": frozenset("""
        --contains --format --merged --no-contains --no-merged --points-at --set-upstream-to --sort
        """.split()),
    "checkout": frozenset("""
        --conflict --inter-hunk-context --orphan --pathspec-from-file --unified
        """.split()),
    "checkout-index": frozenset("""
        --prefix --stage
        """.split()),
    "cherry-pick": frozenset("""
        --cleanup --empty --mainline --strategy --strategy-option
        """.split()),
    "clean": frozenset("""
        --exclude
        """.split()),
    "clone": frozenset("""
        --branch --bundle-uri --config --depth --filter --jobs --origin --ref-format --reference --reference-if-able
        --revision --separate-git-dir --server-option --shallow-exclude --shallow-since --template --upload-pack
        """.split()),
    "commit": frozenset("""
        --author --cleanup --date --file --fixup --inter-hunk-context --message --pathspec-from-file
        --reedit-message --reuse-message --squash --template --trailer --unified
        """.split()),
    "fetch": frozenset("""
        --deepen --depth --filter --jobs --negotiation-tip --recurse-submodules-default --refmap --server-option
        --shallow-exclude --shallow-since --submodule-prefix --upload-pack
        """.split()),
    "format-patch": frozenset("""
        --add-header --base --cc --cover-from-description --creation-factor --description-file --filename-max-length
        --in-reply-to --interdiff --output-directory --range-diff --reroll-count --signature --signature-file
        --start-number --subject-prefix --suffix --to
        """.split()),
    "ls-remote": frozenset("""
        --exec --server-option --sort --upload-pack
        """.split()),
    "merge": frozenset("""
        --cleanup --file --into-name --message --strategy --strategy-option
        """.split()),
    "mv": frozenset("""
        """.split()),
    "pull": frozenset("""
        --cleanup --deepen --depth --negotiation-tip --refmap --server-option --shallow-exclude --shallow-since
        --strategy --strategy-option --upload-pack
        """.split()),
    "read-tree": frozenset("""
        --exclude-per-directory --index-output --prefix --super-prefix
        """.split()),
    "rebase": frozenset("""
        --empty --exec --onto --strategy --strategy-option --whitespace
        """.split()),
    "reset": frozenset("""
        --inter-hunk-context --pathspec-from-file --unified
        """.split()),
    "restore": frozenset("""
        --conflict --inter-hunk-context --pathspec-from-file --source --unified
        """.split()),
    "revert": frozenset("""
        --cleanup --mainline --strategy --strategy-option
        """.split()),
    "rm": frozenset("""
        --pathspec-from-file
        """.split()),
    "tag": frozenset("""
        --cleanup --contains --file --format --local-user --merged --message --no-contains --no-merged --points-at
        --sort --trailer
        """.split()),
    "update-index": frozenset("""
        --cacheinfo --chmod --index-version
        """.split()),
}
