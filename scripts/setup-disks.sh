#!/bin/bash
# Attach the 128 GB SATA SSD as /work + swap.
# If the installer already partitioned it, adopt those filesystems.
# Never wipe a disk that already has a filesystem.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "sudo bash $0" >&2
  exit 1
fi

MIN_WORK=$((90 * 1000 * 1000 * 1000))
MAX_WORK=$((140 * 1000 * 1000 * 1000))
PERSON_USER="${SUDO_USER:-person}"

part_name() {
  local disk="$1" n="$2"
  case "${disk}" in
    nvme*|mmcblk*|loop*) echo "${disk}p${n}" ;;
    *) echo "${disk}${n}" ;;
  esac
}

ensure_work_dirs() {
  install -d -m 0755 /work
  install -d -m 0755 -o "${PERSON_USER}" -g "${PERSON_USER}" /work/person /work/logs /work/npm-cache
}

fstab_has() {
  local uuid="$1"
  grep -q "${uuid}" /etc/fstab
}

root_src="$(findmnt -n -o SOURCE /)"
root_disk="$(lsblk -no PKNAME "${root_src}" 2>/dev/null || true)"
if [[ -z "${root_disk}" ]]; then
  echo "cannot resolve the disk that holds / (SOURCE=${root_src})" >&2
  exit 1
fi

if findmnt -n /work >/dev/null 2>&1; then
  echo "/work already mounted"
  ensure_work_dirs
  exit 0
fi

mapfile -t candidates < <(
  lsblk -dn -b -o NAME,SIZE,TYPE,RM | awk -v min="${MIN_WORK}" -v max="${MAX_WORK}" -v root="${root_disk}" '
    $3 == "disk" && $4 == 0 && $1 != root && $2 >= min && $2 <= max { print $1 }
  '
)

if (( ${#candidates[@]} == 0 )); then
  echo "no 128 GB SATA disk found. /work stays on the root filesystem. root disk is ${root_disk}."
  lsblk -o NAME,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINT
  ensure_work_dirs
  exit 0
fi
if (( ${#candidates[@]} > 1 )); then
  echo "more than one 90-140 GB disk; refusing to guess: ${candidates[*]}" >&2
  exit 1
fi

disk="${candidates[0]}"
dev="/dev/${disk}"
echo "work disk: ${dev} (root is /dev/${root_disk})"

# Anything from this disk already mounted (installer put /home or /work here)?
mapfile -t existing_mounts < <(lsblk -nr -o MOUNTPOINT "${dev}" | awk 'NF && $1 != "" && $1 != "[SWAP]" { print $1 }')
if ((${#existing_mounts[@]} > 0)); then
  echo "${dev} already mounted at ${existing_mounts[*]} — leaving the installer layout alone"
  if [[ " ${existing_mounts[*]} " == *" /work "* ]]; then
    ensure_work_dirs
  fi
  exit 0
fi

work_part=""
swap_part=""
labelled="$(lsblk -nr -o NAME,LABEL "${dev}" | awk '$2=="SPECTREWORK"{print $1; exit}')"
if [[ -n "${labelled}" ]]; then
  work_part="${labelled}"
fi
if [[ -z "${work_part}" ]]; then
  # Largest ext4/xfs/btrfs on this disk.
  work_part="$(lsblk -nr -b -o NAME,FSTYPE,SIZE "${dev}" | awk '
    $2 ~ /^(ext4|xfs|btrfs)$/ { if ($3+0 > max) { max=$3; n=$1 } }
    END { if (n) print n }
  ')"
fi
swap_part="$(lsblk -nr -o NAME,FSTYPE "${dev}" | awk '$2=="swap"{print $1; exit}')"

if [[ -n "${work_part}" || -n "${swap_part}" ]]; then
  echo "adopting installer partitions on ${dev} (no format)"
  if [[ -n "${work_part}" ]]; then
    install -d -m 0755 /work
    work_uuid="$(blkid -s UUID -o value "/dev/${work_part}")"
    if [[ -n "${work_uuid}" ]] && ! fstab_has "${work_uuid}"; then
      printf 'UUID=%s /work ext4 defaults,noatime 0 2\n' "${work_uuid}" >>/etc/fstab
    fi
    mount /work 2>/dev/null || mount "/dev/${work_part}" /work
  fi
  if [[ -n "${swap_part}" ]]; then
    swap_uuid="$(blkid -s UUID -o value "/dev/${swap_part}")"
    if [[ -n "${swap_uuid}" ]] && ! fstab_has "${swap_uuid}"; then
      printf 'UUID=%s none swap sw 0 0\n' "${swap_uuid}" >>/etc/fstab
    fi
    swapon "/dev/${swap_part}" 2>/dev/null || true
  fi
  ensure_work_dirs
  echo "/work ready (adopted ${dev})"
  exit 0
fi

echo "partitioning empty ${dev}: 8G swap + rest ext4 SPECTREWORK"
parted -s "${dev}" mklabel gpt
parted -s "${dev}" mkpart swap linux-swap 1MiB 8193MiB
parted -s "${dev}" mkpart work ext4 8193MiB 100%
udevadm settle || sleep 2
swap_part="$(part_name "${disk}" 1)"
work_part="$(part_name "${disk}" 2)"
mkswap "/dev/${swap_part}"
mkfs.ext4 -F -L SPECTREWORK "/dev/${work_part}"

install -d -m 0755 /work
work_uuid="$(blkid -s UUID -o value "/dev/${work_part}")"
printf 'UUID=%s /work ext4 defaults,noatime 0 2\n' "${work_uuid}" >>/etc/fstab
swap_uuid="$(blkid -s UUID -o value "/dev/${swap_part}")"
printf 'UUID=%s none swap sw 0 0\n' "${swap_uuid}" >>/etc/fstab
swapon "/dev/${swap_part}"
mount /work
ensure_work_dirs
echo "/work ready on ${dev}"
