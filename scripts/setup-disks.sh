#!/bin/bash
# Bind the unused ~128 GB SATA SSD as /work + 8 GB swap. Never touches the
# disk that holds / (that is the 256 GB mSATA). Identify by size, not sda/sdb.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "sudo bash $0" >&2
  exit 1
fi

MIN_WORK=$((90 * 1000 * 1000 * 1000))
MAX_WORK=$((140 * 1000 * 1000 * 1000))

part_name() {
  local disk="$1" n="$2"
  case "${disk}" in
    nvme*|mmcblk*|loop*) echo "${disk}p${n}" ;;
    *) echo "${disk}${n}" ;;
  esac
}

root_src="$(findmnt -n -o SOURCE /)"
root_disk="$(lsblk -no PKNAME "${root_src}" 2>/dev/null || true)"
if [[ -z "${root_disk}" ]]; then
  echo "cannot resolve the disk that holds / (SOURCE=${root_src})" >&2
  exit 1
fi

mapfile -t candidates < <(
  lsblk -dn -b -o NAME,SIZE,TYPE,RM | awk -v min="${MIN_WORK}" -v max="${MAX_WORK}" -v root="${root_disk}" '
    $3 == "disk" && $4 == 0 && $1 != root && $2 >= min && $2 <= max { print $1 }
  '
)

if (( ${#candidates[@]} == 0 )); then
  echo "no unused 128 GB SATA disk found. /work not created. root disk is ${root_disk}."
  lsblk -o NAME,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINT
  exit 0
fi
if (( ${#candidates[@]} > 1 )); then
  echo "more than one 90-140 GB disk; refusing to guess: ${candidates[*]}" >&2
  exit 1
fi

disk="${candidates[0]}"
dev="/dev/${disk}"
echo "work disk: ${dev} (root is /dev/${root_disk})"

if findmnt -n /work >/dev/null 2>&1; then
  echo "/work already mounted"
  install -d -m 0755 -o "${SUDO_USER:-person}" -g "${SUDO_USER:-person}" /work/person /work/logs /work/npm-cache
  exit 0
fi

existing_label="$(lsblk -n -o LABEL "${dev}" | awk 'NF && $1=="SPECTREWORK" {print; exit}')"
work_part=""
swap_part=""
if [[ -n "${existing_label}" ]]; then
  work_part="$(lsblk -nr -o NAME,LABEL "${dev}" | awk '$2=="SPECTREWORK"{print $1; exit}')"
fi

if [[ -z "${work_part}" ]]; then
  mounted="$(lsblk -nr -o MOUNTPOINT "${dev}" | awk 'NF && $1!="" {c++} END {print c+0}')"
  if (( mounted > 0 )); then
    echo "${dev} has mounted partitions. unmount them or pick another disk." >&2
    lsblk "${dev}"
    exit 1
  fi
  fstypes="$(lsblk -nr -o FSTYPE "${dev}" | awk 'NF{c++} END{print c+0}')"
  if (( fstypes > 0 )); then
    echo "${dev} already has filesystems and is not labelled SPECTREWORK." >&2
    echo "leave it alone. wipe it yourself if this really is the 128 GB SATA work disk." >&2
    lsblk -o NAME,SIZE,FSTYPE,LABEL,MOUNTPOINT "${dev}"
    exit 1
  fi

  echo "partitioning ${dev}: 8G swap + rest ext4 SPECTREWORK"
  parted -s "${dev}" mklabel gpt
  parted -s "${dev}" mkpart swap linux-swap 1MiB 8193MiB
  parted -s "${dev}" mkpart work ext4 8193MiB 100%
  udevadm settle || sleep 2
  swap_part="$(part_name "${disk}" 1)"
  work_part="$(part_name "${disk}" 2)"
  mkswap "/dev/${swap_part}"
  mkfs.ext4 -F -L SPECTREWORK "/dev/${work_part}"
fi

if [[ -z "${swap_part}" ]]; then
  swap_part="$(lsblk -nr -o NAME,FSTYPE "${dev}" | awk '$2=="swap"{print $1; exit}')"
fi

install -d -m 0755 /work
work_uuid="$(blkid -s UUID -o value "/dev/${work_part}")"
if ! grep -q "LABEL=SPECTREWORK" /etc/fstab && ! grep -q "${work_uuid}" /etc/fstab; then
  printf 'UUID=%s /work ext4 defaults,noatime 0 2\n' "${work_uuid}" >>/etc/fstab
fi
if [[ -n "${swap_part}" ]]; then
  swap_uuid="$(blkid -s UUID -o value "/dev/${swap_part}")"
  if [[ -n "${swap_uuid}" ]] && ! grep -q "${swap_uuid}" /etc/fstab; then
    printf 'UUID=%s none swap sw 0 0\n' "${swap_uuid}" >>/etc/fstab
  fi
  swapon "/dev/${swap_part}" 2>/dev/null || true
fi
mount /work
PERSON_USER="${SUDO_USER:-person}"
install -d -m 0755 -o "${PERSON_USER}" -g "${PERSON_USER}" /work/person /work/logs /work/npm-cache
echo "/work ready on ${dev}"
