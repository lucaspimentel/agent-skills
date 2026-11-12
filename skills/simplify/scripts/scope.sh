#!/usr/bin/env bash
# Print the scope list for the simplify skill: the resolved target, the changed
# files, and the changed line ranges to review.
#
# Run it with bash:
#
#   scope.sh default      [--path P]...
#   scope.sh pending      [--path P]...
#   scope.sh last-commit  [--path P]...
#   scope.sh diff BASE [END] [--path P]...
#   scope.sh files PATH... [--allow-many]
#
# The caller (SKILL.md) interprets the user's free-form argument, asks about
# ambiguity, and passes explicit flags; this script only does git mechanics.
# It prints the scope list the simplify skill substitutes into its review
# prompt, starting with one "Target:" line.
#
# Exit codes:
#   0  scope printed
#   2  usage error
#   3  not a git repository, or a detached HEAD for the default target
#   4  the base, end, or default branch could not be resolved
#   5  a named path does not exist, matches nothing, or is outside the repository
#   6  a whole-file target matches more than MAX_WHOLE_FILES files
#   7  the scope is empty
set -euo pipefail

EMPTY_TREE=4b825dc642cb6eb9a060e54bf8d69288fbee4904
MAX_WHOLE_FILES=10

usage() {
  cat >&2 <<'EOF'
Usage:
  scope.sh default      [--path P]...
  scope.sh pending      [--path P]...
  scope.sh last-commit  [--path P]...
  scope.sh diff BASE [END] [--path P]...
  scope.sh files PATH... [--allow-many]
EOF
}

die() {
  local code=$1
  shift
  printf 'scope.sh: %s\n' "$*" >&2
  exit "$code"
}

# Use a stable diff regardless of local config.
g() {
  git -c core.quotePath=false -c diff.noprefix=false -c diff.mnemonicPrefix=false "$@"
}

BASE=""
END=""
declare -a PATH_FILTER=()
declare -a PATHSPEC=(--)

# resolve_default_base sets BASE and TARGET_DESC for the default target.
resolve_default_base() {
  local branch
  branch=$(git symbolic-ref -q --short HEAD) || branch=""
  [[ -n $branch ]] || die 3 "HEAD is detached; pass an explicit commit, range, or paths"

  local pr_state="" pr_base_ref="" pr_base_oid="" pr_fields="" pr_lookup=failed
  if command -v gh >/dev/null 2>&1; then
    # gh reports "no pull requests found" on stderr, and that only means the
    # branch has no PR. Capture stderr on the side, because merging it into
    # stdout would corrupt the three lines read below.
    local gh_err="" gh_err_file=""
    gh_err_file=$(mktemp "${TMPDIR:-/tmp}/scope-gh.XXXXXX" 2>/dev/null) || gh_err_file=""
    if pr_fields=$(GH_PROMPT_DISABLED=1 GH_NO_UPDATE_NOTIFIER=1 gh pr view --json state,baseRefName,baseRefOid --jq '.state, .baseRefName, .baseRefOid' 2>"${gh_err_file:-/dev/null}"); then
      pr_lookup=ok
      {
        read -r pr_state
        read -r pr_base_ref
        read -r pr_base_oid
      } <<<"$pr_fields" || true
      [[ $pr_state == null ]] && pr_state=""
      [[ $pr_base_ref == null ]] && pr_base_ref=""
      [[ $pr_base_oid == null ]] && pr_base_oid=""
    elif [[ -n $gh_err_file ]]; then
      gh_err=$(cat "$gh_err_file" 2>/dev/null) || gh_err=""
      if [[ $gh_err == *"no pull requests found"* ]]; then
        pr_lookup=none
      fi
    fi
    [[ -z $gh_err_file ]] || rm -f "$gh_err_file"
  fi

  local pr_note=""
  if [[ $pr_state == OPEN && -n $pr_base_oid ]]; then
    if ! git cat-file -e "${pr_base_oid}^{commit}" 2>/dev/null; then
      local remote=origin
      if git config --get remote.upstream.url >/dev/null 2>&1; then
        remote=upstream
      fi
      git fetch --quiet "$remote" "$pr_base_ref" 2>/dev/null || true
    fi
    if git cat-file -e "${pr_base_oid}^{commit}" 2>/dev/null; then
      BASE=$(git merge-base HEAD "$pr_base_oid") || die 4 "could not find a merge-base with the PR base ${pr_base_oid:0:7}"
      TARGET_DESC="open PR on ${branch} (working tree vs merge-base ${BASE:0:7})"
      return 0
    fi
    pr_note=" (PR base ${pr_base_oid:0:7} unavailable; using the default branch)"
  elif [[ -z $pr_state && $pr_lookup != none ]]; then
    pr_note=" (PR detection unavailable; using the default branch)"
  fi

  local ref="" name=""
  ref=$(git symbolic-ref -q --short refs/remotes/origin/HEAD 2>/dev/null) || ref=""
  if [[ -z $ref ]] && command -v gh >/dev/null 2>&1; then
    name=$(GH_PROMPT_DISABLED=1 GH_NO_UPDATE_NOTIFIER=1 gh repo view --json defaultBranchRef --jq '.defaultBranchRef.name' 2>/dev/null) || name=""
    [[ $name == null ]] && name=""
    if [[ -n $name ]]; then
      ref="origin/$name"
    fi
  fi
  if [[ -z $ref ]]; then
    local candidate
    for candidate in origin/main origin/master; do
      if git rev-parse --verify -q "$candidate" >/dev/null 2>&1; then
        ref=$candidate
        break
      fi
    done
  fi
  [[ -n $ref ]] || die 4 "could not resolve the default branch; set refs/remotes/origin/HEAD or pass an explicit target"

  BASE=$(git merge-base HEAD "$ref") || die 4 "could not find a merge-base between HEAD and $ref"
  TARGET_DESC="default branch (working tree vs merge-base ${BASE:0:7} with ${ref})${pr_note}"
}

declare -a FILE_STATUS=()
declare -a FILE_PATH=()
CHANGED_SINCE_END=""

# Load once per run the files whose working-tree content differs from END:
# their ranges describe END's version, so they would not match the current
# file. A per-file `git diff --quiet` would cost one process per file.
load_changed_since_end() {
  CHANGED_SINCE_END=$(g diff --name-only "$END" 2>/dev/null) || CHANGED_SINCE_END=""
}

changed_since_end() {
  case $'\n'"$CHANGED_SINCE_END"$'\n' in
    *$'\n'"$1"$'\n'*) return 0 ;;
  esac
  return 1
}

list_changed_files() {
  local tok
  while IFS= read -r -d '' tok; do
    case $tok in
      R[0-9]* | C[0-9]*)
        local old="" new=""
        IFS= read -r -d '' old || true
        IFS= read -r -d '' new || true
        FILE_STATUS+=("${tok:0:1}")
        FILE_PATH+=("$new")
        ;;
      [A-Z])
        local path=""
        IFS= read -r -d '' path || true
        # Read the path token either way so the NUL-separated stream stays
        # aligned, then skip deletions: the file is gone, so there is nothing
        # to simplify. A diff made only of deletions ends up with no files.
        if [[ $tok == D ]]; then
          continue
        fi
        FILE_STATUS+=("$tok")
        FILE_PATH+=("$path")
        ;;
    esac
  done < <(g diff --name-status -z -M "$BASE" ${END:+"$END"} "${PATHSPEC[@]}")
}

list_untracked_files() {
  local path
  while IFS= read -r -d '' path; do
    FILE_STATUS+=("A")
    FILE_PATH+=("$path")
  done < <(git ls-files -z --others --exclude-standard "${PATHSPEC[@]}")
}

# compute_ranges prints "12, 30-45", "UNAVAILABLE", or "DELETIONS" for one path.
compute_ranges() {
  local path=$1
  local diff_out=""
  diff_out=$(g diff -U0 --no-color --no-ext-diff "$BASE" ${END:+"$END"} -- "$path") || {
    printf 'UNAVAILABLE'
    return 0
  }

  local -a starts=()
  local -a ends=()
  local saw_hunk=0
  local saw_binary=0
  local line c d
  while IFS= read -r line; do
    line=${line%$'\r'}
    if [[ $line == "Binary files "* || $line == "GIT binary patch"* ]]; then
      saw_binary=1
    elif [[ $line =~ ^@@\ -([0-9]+)(,([0-9]+))?\ \+([0-9]+)(,([0-9]+))?\ @@ ]]; then
      saw_hunk=1
      c=${BASH_REMATCH[4]}
      d=${BASH_REMATCH[6]:-1}
      if [[ $d != 0 ]]; then
        starts+=("$c")
        ends+=("$((c + d - 1))")
      fi
    fi
  done <<<"$diff_out"

  if [[ $saw_hunk == 0 ]]; then
    if [[ $saw_binary == 1 ]]; then
      printf 'UNAVAILABLE'
    else
      printf 'DELETIONS'
    fi
    return 0
  fi

  # Every hunk was a pure deletion. Return before expanding the empty arrays,
  # because `set -u` plus an empty `"${arr[@]}"` is an error in bash 3.2, which
  # macOS still ships.
  if [[ ${#starts[@]} -eq 0 ]]; then
    printf 'DELETIONS'
    return 0
  fi

  # Merge ranges that touch or overlap.
  local -a merged_starts=()
  local -a merged_ends=()
  local i range_start range_end
  for i in "${!starts[@]}"; do
    range_start=${starts[$i]}
    range_end=${ends[$i]}
    if [[ ${#merged_starts[@]} -gt 0 ]]; then
      local last_index=$(( ${#merged_starts[@]} - 1 ))
      if ((range_start <= merged_ends[last_index] + 1)); then
        if ((range_end > merged_ends[last_index])); then
          merged_ends[last_index]=$range_end
        fi
        continue
      fi
    fi
    merged_starts+=("$range_start")
    merged_ends+=("$range_end")
  done

  local -a parts=()
  for i in "${!merged_starts[@]}"; do
    if [[ ${merged_starts[$i]} == "${merged_ends[$i]}" ]]; then
      parts+=("${merged_starts[$i]}")
    else
      parts+=("${merged_starts[$i]}-${merged_ends[$i]}")
    fi
  done

  local joined=""
  local part
  for part in "${parts[@]}"; do
    if [[ -z $joined ]]; then
      joined=$part
    else
      joined="${joined}, ${part}"
    fi
  done
  printf '%s' "$joined"
}

print_scope_list() {
  local i path status ranges
  for i in "${!FILE_PATH[@]}"; do
    path=${FILE_PATH[$i]}
    status=${FILE_STATUS[$i]}
    if [[ $status == A ]]; then
      printf -- '- %s (added; entire file is in scope)\n' "$path"
      continue
    fi
    if [[ -n $END ]] && changed_since_end "$path"; then
      printf -- '- %s (%s; changed lines unavailable, inspect git diff before editing)\n' "$path" "$status"
      continue
    fi
    ranges=$(compute_ranges "$path")
    case $ranges in
      UNAVAILABLE)
        printf -- '- %s (%s; changed lines unavailable, inspect git diff before editing)\n' "$path" "$status"
        ;;
      DELETIONS)
        printf -- '- %s (%s; deletions only, no current lines to simplify)\n' "$path" "$status"
        ;;
      *)
        printf -- '- %s (%s; changed lines: %s)\n' "$path" "$status" "$ranges"
        ;;
    esac
  done
}

print_target_line() {
  local desc=$1
  if [[ ${#PATH_FILTER[@]} -gt 0 ]]; then
    local joined
    joined=$(printf '%s, ' "${PATH_FILTER[@]}")
    desc="${desc} [paths: ${joined%, }]"
  fi
  printf 'Target: %s\n' "$desc"
}

# ---------------------------------------------------------------------------
# Whole-file targets
# ---------------------------------------------------------------------------

declare -a WHOLE_FILES=()

dedup_whole_files() {
  [[ ${#WHOLE_FILES[@]} -gt 0 ]] || return 0
  local sorted
  sorted=$(printf '%s\n' "${WHOLE_FILES[@]}" | LC_ALL=C sort -u)
  WHOLE_FILES=()
  local p
  while IFS= read -r p; do
    if [[ -n $p ]]; then
      WHOLE_FILES+=("$p")
    fi
  done <<<"$sorted"
}

# Git pathspecs are not shell globs: '**/x' skips root-level files and '*'
# crosses directories. ':(glob)' restores the globstar semantics the skill
# documents, so apply it whenever the argument actually contains a wildcard.
pathspec_for() {
  case $1 in
    *\** | *\?* | *\[*) printf ':(glob)%s' "$1" ;;
    *) printf '%s' "$1" ;;
  esac
}

# to_repo_relative prints an existing path relative to the repository root, or
# fails when it lies outside this repository (including a nested repository).
to_repo_relative() {
  local p=$1 dir name="" top prefix
  if [[ -d $p ]]; then
    dir=$p
  else
    dir=${p%[/\\]*}
    if [[ $dir == "$p" ]]; then
      dir=.
    elif [[ -z $dir ]]; then
      dir=/
    fi
    name=${p##*[/\\]}
  fi
  top=$(cd "$dir" 2>/dev/null && git rev-parse --show-toplevel 2>/dev/null) || return 1
  [[ $top == "$REPO_ROOT" ]] || return 1
  prefix=$(cd "$dir" && git rev-parse --show-prefix 2>/dev/null) || return 1
  p=${prefix}${name}
  p=${p%/}
  printf '%s' "${p:-.}"
}

collect_whole_files() {
  local arg matched p rel spec
  for arg in "$@"; do
    # Normalize an existing path through git, which handles symlinks, Windows
    # drive paths, and '..' segments, and rejects anything outside the repo.
    if [[ -e $arg ]]; then
      rel=$(to_repo_relative "$arg") || die 5 "path is outside the repository: $arg"
      if [[ -f $rel ]]; then
        WHOLE_FILES+=("$rel")
        continue
      fi
      arg=$rel
    fi
    spec=$(pathspec_for "$arg")
    # Tracked files that are gone from the working tree are not in scope.
    matched=0
    while IFS= read -r -d '' p; do
      if [[ -e $p ]]; then
        WHOLE_FILES+=("$p")
        matched=1
      fi
    done < <(git ls-files -z -- "$spec")
    while IFS= read -r -d '' p; do
      WHOLE_FILES+=("$p")
      matched=1
    done < <(git ls-files -z --others --exclude-standard -- "$spec")
    if [[ ! -d $arg && $matched == 0 ]]; then
      die 5 "path does not exist or matches nothing: $arg"
    fi
  done
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

[[ $# -gt 0 ]] || {
  usage
  exit 2
}

MODE=$1
shift

REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || die 3 "not inside a git repository"
REPO_PREFIX=$(git rev-parse --show-prefix 2>/dev/null) || REPO_PREFIX=""
cd "$REPO_ROOT" || exit 3

ALLOW_MANY=no
declare -a WHOLE_ARGS=()

case $MODE in
  default | pending | last-commit)
    while [[ $# -gt 0 ]]; do
      case $1 in
        --path)
          [[ $# -ge 2 ]] || die 2 "--path requires a value"
          PATH_FILTER+=("$2")
          shift 2
          ;;
        *)
          die 2 "unexpected argument: $1"
          ;;
      esac
    done
    ;;
  diff)
    while [[ $# -gt 0 ]]; do
      case $1 in
        --path)
          [[ $# -ge 2 ]] || die 2 "--path requires a value"
          PATH_FILTER+=("$2")
          shift 2
          ;;
        -*)
          die 2 "unexpected argument: $1"
          ;;
        *)
          if [[ -z $BASE ]]; then
            BASE=$1
          elif [[ -z $END ]]; then
            END=$1
          else
            die 2 "too many positional arguments: $1"
          fi
          shift
          ;;
      esac
    done
    [[ -n $BASE ]] || die 2 "diff requires a base revision"
    git rev-parse --verify -q "${BASE}^{commit}" >/dev/null 2>&1 || die 4 "not a commit: $BASE"
    if [[ -n $END ]]; then
      git rev-parse --verify -q "${END}^{commit}" >/dev/null 2>&1 || die 4 "not a commit: $END"
      END=$(git rev-parse "${END}^{commit}")
    fi
    BASE=$(git rev-parse "${BASE}^{commit}")
    ;;
  files)
    while [[ $# -gt 0 ]]; do
      case $1 in
        --allow-many)
          ALLOW_MANY=yes
          shift
          ;;
        -*)
          die 2 "unexpected argument: $1"
          ;;
        *)
          WHOLE_ARGS+=("$1")
          shift
          ;;
      esac
    done
    [[ ${#WHOLE_ARGS[@]} -gt 0 ]] || die 2 "files requires at least one path"
    ;;
  *)
    usage
    exit 2
    ;;
esac

# Rebase a user-supplied path onto the repository root, since the script runs
# from the root but the caller may have named paths relative to a subdirectory.
rebase_path() {
  local p=$1
  case $p in
    /* | [A-Za-z]:[/\\]* | :/* | :\(* | :!*) printf '%s' "$p" ;;
    *) printf '%s%s' "$REPO_PREFIX" "$p" ;;
  esac
}

if [[ -n $REPO_PREFIX ]]; then
  if [[ ${#PATH_FILTER[@]} -gt 0 ]]; then
    for i in "${!PATH_FILTER[@]}"; do
      PATH_FILTER[$i]=$(rebase_path "${PATH_FILTER[$i]}")
    done
  fi
  if [[ ${#WHOLE_ARGS[@]} -gt 0 ]]; then
    for i in "${!WHOLE_ARGS[@]}"; do
      WHOLE_ARGS[$i]=$(rebase_path "${WHOLE_ARGS[$i]}")
    done
  fi
fi

if [[ ${#PATH_FILTER[@]} -gt 0 ]]; then
  PATHSPEC=(-- "${PATH_FILTER[@]}")
fi

# ---------------------------------------------------------------------------
# Resolve the target
# ---------------------------------------------------------------------------

if [[ $MODE == files ]]; then
  collect_whole_files "${WHOLE_ARGS[@]}"
  dedup_whole_files
  count=${#WHOLE_FILES[@]}
  if [[ $count -eq 0 ]]; then
    die 7 "no files to simplify for the resolved target"
  fi
  if [[ $count -gt $MAX_WHOLE_FILES && $ALLOW_MANY != yes ]]; then
    printf 'scope.sh: whole-file target matches %s files (max %s); rerun with --allow-many to accept:\n' "$count" "$MAX_WHOLE_FILES" >&2
    printf '  %s\n' "${WHOLE_FILES[@]}" >&2
    exit 6
  fi
  printf 'Target: whole-file scope (%s files)\n' "$count"
  for path in "${WHOLE_FILES[@]}"; do
    printf -- '- %s (whole file in scope)\n' "$path"
  done
  exit 0
fi

case $MODE in
  default)
    resolve_default_base
    print_target_line "$TARGET_DESC"
    list_changed_files
    list_untracked_files
    ;;
  pending)
    if git rev-parse --verify -q HEAD >/dev/null 2>&1; then
      BASE=$(git rev-parse HEAD)
      print_target_line "pending changes (working tree vs HEAD ${BASE:0:7})"
    else
      BASE=$EMPTY_TREE
      print_target_line "pending changes (unborn HEAD; entire tree is new)"
    fi
    END=""
    list_changed_files
    list_untracked_files
    ;;
  last-commit)
    git rev-parse --verify -q HEAD >/dev/null 2>&1 || die 4 "no commits to diff"
    END=$(git rev-parse HEAD)
    if BASE=$(git rev-parse --verify -q "HEAD^" 2>/dev/null); then
      print_target_line "last commit (${BASE:0:7}..${END:0:7})"
    else
      BASE=$EMPTY_TREE
      print_target_line "last commit (root commit; entire tree is new)"
    fi
    list_changed_files
    ;;
  diff)
    if [[ -n $END ]]; then
      print_target_line "${BASE:0:7}..${END:0:7}"
    else
      print_target_line "${BASE:0:7} vs working tree"
    fi
    list_changed_files
    if [[ -z $END ]]; then
      list_untracked_files
    fi
    ;;
esac

if [[ ${#FILE_PATH[@]} -eq 0 ]]; then
  die 7 "no changes to simplify for the resolved target"
fi

if [[ -n $END ]]; then
  load_changed_since_end
fi

print_scope_list
