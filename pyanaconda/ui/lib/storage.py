#
# Copyright (C) 2019  Red Hat, Inc.
#
# This copyrighted material is made available to anyone wishing to use,
# modify, copy, or redistribute it subject to the terms and conditions of
# the GNU General Public License v.2, or (at your option) any later version.
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY expressed or implied, including the implied warranties of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General
# Public License for more details.  You should have received a copy of the
# GNU General Public License along with this program; if not, write to the
# Free Software Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301, USA.  Any Red Hat trademarks that are incorporated in the
# source code or documentation are not subject to the GNU General Public
# License and may only be used or replicated with the express permission of
# Red Hat, Inc.
#
from blivet.size import Size
from dasbus.error import DBusError

from dasbus.typing import unwrap_variant
from dasbus.client.proxy import get_object_path

from pyanaconda.anaconda_loggers import get_module_logger
from pyanaconda.core.constants import PARTITIONING_METHOD_AUTOMATIC, BOOTLOADER_DRIVE_UNSET
from pyanaconda.core.i18n import P_, _
from pyanaconda.errors import errorHandler as error_handler, ERROR_RAISE
from pyanaconda.flags import flags
from pyanaconda.modules.common.constants.objects import DISK_SELECTION, BOOTLOADER, DEVICE_TREE, \
    DISK_INITIALIZATION
from pyanaconda.modules.common.constants.services import STORAGE
from pyanaconda.modules.common.errors.configuration import StorageConfigurationError, \
    BootloaderConfigurationError
from pyanaconda.modules.common.structures.validation import ValidationReport
from pyanaconda.modules.common.task import sync_run_task
from pyanaconda.storage.utils import filter_disks_by_names

log = get_module_logger(__name__)


def create_partitioning(partitioning_method):
    """Create a partitioning.

    :param partitioning_method: a partitioning method
    :return: a proxy of a partitioning module
    """
    storage_proxy = STORAGE.get_proxy()
    object_path = storage_proxy.CreatePartitioning(
        partitioning_method
    )
    return STORAGE.get_proxy(object_path)


def find_partitioning():
    """Find a partitioning to use or create a new one.

    :return: a proxy of a partitioning module
    """
    storage_proxy = STORAGE.get_proxy()
    object_paths = storage_proxy.CreatedPartitioning

    if object_paths:
        # Choose the last created partitioning.
        object_path = object_paths[-1]
    else:
        # Or create the automatic partitioning.
        object_path = storage_proxy.CreatePartitioning(
            PARTITIONING_METHOD_AUTOMATIC
        )

    return STORAGE.get_proxy(object_path)


def reset_storage(scan_all=False, retry=True):
    """Reset the storage model.

    :param scan_all: should we scan all devices in the system?
    :param retry: should we allow to retry the reset?
    """
    # Clear the exclusive disks to scan all devices in the system.
    if scan_all:
        disk_select_proxy = STORAGE.get_proxy(DISK_SELECTION)
        disk_select_proxy.SetExclusiveDisks([])

    # Scan the devices.
    storage_proxy = STORAGE.get_proxy()

    while True:
        try:
            task_path = storage_proxy.ScanDevicesWithTask()
            task_proxy = STORAGE.get_proxy(task_path)
            sync_run_task(task_proxy)
        except DBusError as e:
            # Is the retry allowed?
            if not retry:
                raise
            # Does the user want to retry?
            elif error_handler.cb(e) == ERROR_RAISE:
                raise
            # Retry the storage reset.
            else:
                continue
        else:
            # No need to retry.
            break

    # Reset the partitioning.
    storage_proxy.ResetPartitioning()


def reset_bootloader():
    """Reset the bootloader."""
    bootloader_proxy = STORAGE.get_proxy(BOOTLOADER)
    bootloader_proxy.SetDrive(BOOTLOADER_DRIVE_UNSET)


def select_default_disks():
    """Select default disks for the partitioning.

    If there are some disks already selected, do nothing.
    In the automatic installation, select all disks. In
    the interactive installation, select a disk if there
    is only one available.

    :return: a list of selected disks
    """
    disk_select_proxy = STORAGE.get_proxy(DISK_SELECTION)
    selected_disks = disk_select_proxy.SelectedDisks
    ignored_disks = disk_select_proxy.IgnoredDisks

    if selected_disks:
        # Do nothing if there are some disks selected.
        pass
    elif flags.automatedInstall:
        # Get all disks.
        device_tree = STORAGE.get_proxy(DEVICE_TREE)
        all_disks = device_tree.GetDisks()

        # Select all disks.
        selected_disks = [d for d in all_disks if d not in ignored_disks]
        disk_select_proxy.SetSelectedDisks(selected_disks)
        log.debug("Selecting all disks by default: %s", ",".join(selected_disks))
    else:
        # Get usable disks.
        usable_disks = disk_select_proxy.GetUsableDisks()
        available_disks = [d for d in usable_disks if d not in ignored_disks]

        # Select a usable disk if there is only one available.
        if len(available_disks) == 1:
            selected_disks = available_disks
            apply_disk_selection(selected_disks)

        log.debug("Selecting one or less disks by default: %s", ",".join(selected_disks))

    return selected_disks


def apply_disk_selection(selected_names, reset_boot_drive=False):
    """Apply the disks selection.

    :param selected_names: a list of selected disk names
    :param reset_boot_drive: reset the boot drive if it is not selected
    """
    device_tree = STORAGE.get_proxy(DEVICE_TREE)

    # Get disks.
    disks = set(device_tree.GetDisks())
    selected_disks = filter_disks_by_names(disks, selected_names)

    # Get ancestors.
    ancestors_names = device_tree.GetAncestors(selected_disks)
    ancestors_disks = filter_disks_by_names(disks, ancestors_names)

    # Set the disks to select.
    disk_select_proxy = STORAGE.get_proxy(DISK_SELECTION)
    disk_select_proxy.SetSelectedDisks(selected_names + ancestors_disks)

    # Set the drives to clear.
    disk_init_proxy = STORAGE.get_proxy(DISK_INITIALIZATION)
    disk_init_proxy.SetDrivesToClear(selected_names)

    # Reset the boot drive if it is not selected.
    # FIXME: Move this logic the Storage module?
    if reset_boot_drive:
        boot_loader = STORAGE.get_proxy(BOOTLOADER)
        boot_drive = boot_loader.Drive

        if boot_drive and boot_drive not in selected_names:
            reset_bootloader()


def get_disks_summary(disks):
    """Get a summary of the selected disks

    :param disks: a list of names of selected disks
    :return: a string with a summary
    """
    device_tree = STORAGE.get_proxy(DEVICE_TREE)

    count = len(disks)
    capacity = Size(device_tree.GetDiskTotalSpace(disks))
    free_space = Size(device_tree.GetDiskFreeSpace(disks))

    return P_(
        "{count} disk selected; {capacity} capacity; {free} free",
        "{count} disks selected; {capacity} capacity; {free} free",
        count).format(count=count, capacity=capacity, free=free_space)


def mark_protected_device(spec):
    """Mark a device as protected.

    :param spec: a specification of the device
    """
    disk_selection_proxy = STORAGE.get_proxy(DISK_SELECTION)
    protected_devices = disk_selection_proxy.ProtectedDevices

    if spec not in protected_devices:
        protected_devices.add(spec)

    disk_selection_proxy.SetProtectedDevices(protected_devices)


def unmark_protected_device(spec):
    """Unmark a device as protected.

    :param spec: a specification of the device
    """
    disk_selection_proxy = STORAGE.get_proxy(DISK_SELECTION)
    protected_devices = disk_selection_proxy.ProtectedDevices

    if spec in protected_devices:
        protected_devices.remove(spec)

    disk_selection_proxy.SetProtectedDevices(protected_devices)


def try_populate_devicetree():
    """Try to populate a device tree.

    Try to populate the devic etree while catching errors and dealing with
    some special ones in a nice way (giving user chance to do something about
    them).
    """
    device_tree = STORAGE.get_proxy(DEVICE_TREE)

    while True:
        try:
            task_path = device_tree.FindDevicesWithTask()
            task_proxy = STORAGE.get_proxy(task_path)
            sync_run_task(task_proxy)
        except DBusError as e:
            if error_handler.cb(e) == ERROR_RAISE:
                raise
            else:
                continue
        else:
            break


def apply_partitioning(partitioning, show_message):
    """Apply the given partitioning.

    :param partitioning: a DBus proxy of a partitioning
    :param show_message: a callback for showing a message
    :return: an instance of ValidationReport
    """
    report = ValidationReport()

    try:
        show_message(_("Saving storage configuration..."))
        task_path = partitioning.ConfigureWithTask()
        task_proxy = STORAGE.get_proxy(task_path)
        sync_run_task(task_proxy)
    except StorageConfigurationError as e:
        show_message(_("Failed to save storage configuration"))
        report.error_messages.append(str(e))
        reset_bootloader()
        reset_storage(scan_all=True)
    except BootloaderConfigurationError as e:
        show_message(_("Failed to save boot loader configuration"))
        report.error_messages.append(str(e))
        reset_bootloader()
    else:
        show_message(_("Checking storage configuration..."))
        task_path = partitioning.ValidateWithTask()
        task_proxy = STORAGE.get_proxy(task_path)
        sync_run_task(task_proxy)

        result = unwrap_variant(task_proxy.GetResult())
        report = ValidationReport.from_structure(result)

        if report.is_valid():
            storage_proxy = STORAGE.get_proxy()
            storage_proxy.ApplyPartitioning(
                get_object_path(partitioning)
            )

    return report


def is_local_disk(device_type):
    """Is the disk local?

    A local disk doesn't require any additional setup unlike
    the advanced storage.

    While technically local disks, zFCP and NVDIMM devices are
    specialized storage and should not be considered local.

    :param device_type: a device type
    :return: True or False
    """
    return device_type not in (
        "dm-multipath",
        "iscsi",
        "fcoe",
        "zfcp",
        "nvdimm"
    )