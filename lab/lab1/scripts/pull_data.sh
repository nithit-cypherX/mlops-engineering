#!/bin/sh
# Run in a disposable container; keep credentials and the main DVC config untouched.
set -eu

config_file=$1
metadata_file=$2
output_dir=$3
work_dir=$(mktemp -d)
export HOME="$work_dir/home"
export DVC_NO_ANALYTICS=1
export AZURE_STORAGE_ANON=true
# The mounted-folder UID may not have an entry in the image's /etc/passwd.
# DVC uses getpass.getuser(); this supplies a name without changing the UID.
export USER=runner LOGNAME=runner
mkdir -p "$HOME" "$work_dir/data"
cd "$work_dir"

# Git and a host Python environment are not needed for this download.
dvc init --no-scm -q
cp "$config_file" .dvc/config
dvc config core.no_scm true
dvc remote modify storage allow_anonymous_login true
cp "$metadata_file" data/raw.dvc
dvc pull

# Only publish the dataset after DVC has completed the download.
mkdir -p "$output_dir/raw"
cp data/raw/sensors.csv "$output_dir/raw/sensors.csv"
