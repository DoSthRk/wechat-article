#!/bin/sh
# Bundle is produced from the exact reviewed and pushed commit. No live edits.
set -eu
revision=$1
expected=$2
task_root=$3
test ${#revision} -eq 40
test ${#expected} -eq 40
case "$revision$expected" in *[!a-f0-9]*) exit 1;; esac
case "$task_root" in /tmp/gm-editorial.*) ;; *) exit 1;; esac
article_root=/opt/gm-apps/article
old_release=$(readlink -f "$article_root/current")
test "$(git --git-dir="$article_root/repo.git" rev-parse main)" = "$expected"
test "$(cat "$task_root/expected-release")" = "$old_release"
new_release=$article_root/releases/editorial-$(printf '%s' "$revision" | cut -c1-12)
test ! -e "$new_release"
git --git-dir="$article_root/repo.git" fetch "$task_root/release.bundle" main:main
test "$(git --git-dir="$article_root/repo.git" rev-parse main)" = "$revision"
mkdir -m 2755 "$new_release"
git --git-dir="$article_root/repo.git" archive "$revision" | tar -x -C "$new_release"
mv "$new_release/inputs" "$new_release/inputs.release-template"
for directory in inputs outputs runtime logs; do
    test ! -e "$new_release/$directory"
    ln -s "../../shared/$directory" "$new_release/$directory"
done
ln -s "$(readlink -f "$old_release/.venv")" "$new_release/.venv"
ln -s "$(readlink -f "$old_release/.env")" "$new_release/.env"
touch "$new_release/deploy/editorial-enabled"
cd "$task_root"
"$new_release/.venv/bin/python" -B "$new_release/deploy/verify_editorial_release.py" --source "$new_release" --work-root "$task_root"
"$old_release/.venv/bin/python" -B "$new_release/deploy/probe_editorial_release.py" before "$task_root"
sudo -n /usr/bin/systemctl stop gm-blog.service
next_link=$article_root/current.editorial
test ! -e "$next_link"
ln -s "$new_release" "$next_link"
mv -T "$next_link" "$article_root/current"
if sudo -n /usr/bin/systemctl restart gm-blog.service &&
   "$new_release/.venv/bin/python" -B "$new_release/deploy/probe_editorial_release.py" after "$task_root"; then
    printf '%s\n' "Activated $new_release"
else
    sudo -n /usr/bin/systemctl stop gm-blog.service
    ln -s "$old_release" "$next_link"
    mv -T "$next_link" "$article_root/current"
    sudo -n /usr/bin/systemctl restart gm-blog.service
    printf '%s\n' "Validation failed; restored $old_release; retain shared editorial state" >&2
    exit 1
fi
