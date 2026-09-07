"""Domain-specific exceptions."""


class AthenaError(RuntimeError):
    """Base error shown to command-line users without a traceback."""


class RpcError(AthenaError):
    """The router rejected or did not understand a JSON-RPC request."""


class TelnetError(AthenaError):
    """A Telnet connection or shell operation failed."""


class DeviceMismatchError(AthenaError):
    """The connected block layout does not look like an Athena AX6600."""


class TransferError(AthenaError):
    """A router-to-PC transfer failed or was incomplete."""


class IntegrityError(AthenaError):
    """A downloaded artifact did not match its remote checksum or size."""


class FlashError(AthenaError):
    """A U-Boot preflight, upload, write, or readback check failed."""


class UbootEnterError(AthenaError):
    """The network U-Boot interrupt workflow could not complete."""


class OperationCancelled(AthenaError):
    """The user safely cancelled a cancellable workflow stage."""


class FirmwareFlashError(AthenaError):
    """Factory image validation, U-Boot upload, or firmware write failed."""


class PartitionResizeError(AthenaError):
    """GPT validation, generation, upload, or partition-table write failed."""


class OfficialFirmwareUpgradeError(AthenaError):
    """The guarded original-firmware recovery workflow failed."""
