"""
Telops-specific register addresses and IntEnum value mappings for the FAST M3k camera.

All constants are derived from the camera's GenICam XML descriptor
(CommonTEL2000LibProject.xml v12.7.1, downloaded directly from the camera).
Register values are 4 bytes each; integers use big-endian uint32 (>I) encoding
and floats use big-endian IEEE 754 (>f) encoding.
"""

from __future__ import annotations

from enum import IntEnum

# ============================================================
# Image Format
# ============================================================
REG_WIDTH = 0xD300
REG_HEIGHT = 0xD304  # Includes 2 header rows
REG_PIXEL_FORMAT = 0xD308
REG_PAYLOAD_SIZE = 0xD30C
REG_ACQUISITION_MODE = 0xD310
REG_ACQUISITION_START = 0xD314  # Command
REG_ACQUISITION_STOP = 0xD318  # Command
REG_ACQUISITION_ARM = 0xE800  # Command

# ============================================================
# Exposure & Frame Rate
# ============================================================
REG_EXPOSURE_MODE = 0xE804
REG_EXPOSURE_TIME = 0xE808  # Float, microseconds
REG_EXPOSURE_AUTO = 0xE82C  # ExposureAuto: 0=Off, 1=Once, 2=Continuous
REG_ACQUISITION_FRAME_RATE = 0xE810  # Float, Hz
REG_EXPOSURE_TIME_1 = 0xE84C  # EHDRI exposure 1
REG_EXPOSURE_TIME_2 = 0xE850
REG_EXPOSURE_TIME_3 = 0xE854
REG_EXPOSURE_TIME_4 = 0xE858

# ============================================================
# Detector / Sensor
# ============================================================
REG_SENSOR_WELL_DEPTH = 0xE8DC
REG_INTEGRATION_MODE = 0xE8E0
REG_CALIBRATION_MODE = 0xE86C
REG_DETECTOR_MODE = 0xEC38

# ============================================================
# Trigger
# ============================================================
REG_TRIGGER_SELECTOR = 0xE8F0
REG_TRIGGER_MODE = 0xE8F4
REG_TRIGGER_SOFTWARE = 0xE8F8  # Command
REG_TRIGGER_SOURCE = 0xE8FC
REG_TRIGGER_ACTIVATION = 0xE900
REG_TRIGGER_DELAY = 0xE904  # Float, microseconds

# ============================================================
# Memory Buffer (16GB onboard)
# ============================================================
REG_MEMORY_BUFFER_MODE = 0xE908
REG_MEMORY_BUFFER_NUM_IMAGES_MAX = 0xE90C
REG_MEMORY_BUFFER_NUM_SEQ_MAX = 0xE910
REG_MEMORY_BUFFER_SEQ_COUNT = 0xE914
REG_MEMORY_BUFFER_NUM_SEQUENCES = 0xE918
REG_MEMORY_BUFFER_SEQ_SIZE_MAX = 0xE91C
REG_MEMORY_BUFFER_SEQ_SIZE = 0xE920
REG_MEMORY_BUFFER_PRE_MOI_SIZE = 0xE924
REG_MEMORY_BUFFER_SEQ_SELECTOR = 0xE928
REG_MEMORY_BUFFER_SEQ_FIRST_FRAME_ID = 0xE92C
REG_MEMORY_BUFFER_SEQ_MOI_FRAME_ID = 0xE930
REG_MEMORY_BUFFER_SEQ_RECORDED_SIZE = 0xE934
REG_MEMORY_BUFFER_DOWNLOAD_IMAGE_FRAME_ID = 0xE938
REG_MEMORY_BUFFER_DOWNLOAD_MODE = 0xE93C
REG_MEMORY_BUFFER_CLEAR_ALL = 0xE940  # Command

# Memory Buffer - Extended
REG_MEMORY_BUFFER_MOI_SOURCE = 0xEB2C
REG_MEMORY_BUFFER_MOI_ACTIVATION = 0xEB30
REG_MEMORY_BUFFER_MOI_SOFTWARE = 0xEB34  # Command
REG_MEMORY_BUFFER_STATUS = 0xEBB0
REG_MEMORY_BUFFER_SEQ_CLEAR = 0xEB98  # Command
REG_MEMORY_BUFFER_SEQ_DEFRAG = 0xEB9C  # Command
REG_MEMORY_BUFFER_SEQ_WIDTH = 0xEB90
REG_MEMORY_BUFFER_SEQ_HEIGHT = 0xEB94
REG_MEMORY_BUFFER_SEQ_OFFSET_X = 0xEB88
REG_MEMORY_BUFFER_SEQ_OFFSET_Y = 0xEB8C
REG_MEMORY_BUFFER_DOWNLOAD_FRAME_COUNT = 0xEBAC
REG_MEMORY_BUFFER_DOWNLOAD_FRAME_ID = 0xEBA8
REG_MEMORY_BUFFER_DOWNLOAD_BITRATE_MAX = 0xEAD4  # Float
REG_MEMORY_BUFFER_TOTAL_SPACE_HIGH = 0xEB80
REG_MEMORY_BUFFER_TOTAL_SPACE_LOW = 0xEB84
REG_MEMORY_BUFFER_FREE_SPACE_HIGH = 0xEB70
REG_MEMORY_BUFFER_FREE_SPACE_LOW = 0xEB74

# ============================================================
# Device Control / Status
# ============================================================
REG_DEVICE_RESET = 0xD340  # Command (was incorrectly 0xE948)
REG_DEVICE_POWER_STATE_SETPOINT = 0xE948  # Enum: DevicePowerState
REG_DEVICE_POWER_STATE = 0xE94C
REG_DEVICE_LED = 0xE950
REG_DEVICE_NOT_READY = 0xEA84
REG_TDC_STATUS = 0xEAAC  # Int, bitmask (RO, NoCache)
REG_DEVICE_TEMPERATURE = 0xE970  # Float, Celsius

# TDC Status bit flags
TDC_WAITING_FOR_COOLER = 0x0001
TDC_WAITING_FOR_SENSOR = 0x0002
TDC_WAITING_FOR_INIT = 0x0004
TDC_WAITING_FOR_ICU = 0x0010
TDC_WAITING_FOR_ND_FILTER = 0x0020
TDC_WAITING_FOR_CAL_INIT = 0x0040
TDC_WAITING_FOR_FILTER_WHEEL = 0x0080
TDC_WAITING_FOR_ARM = 0x0100
TDC_WAITING_FOR_VALID_PARAMS = 0x0200
TDC_ACQUISITION_STARTED = 0x0400
TDC_WAITING_FOR_CAL_DATA = 0x1000
TDC_WAITING_FOR_IMAGE_CORRECTION = 0x2000
TDC_WAITING_FOR_OUTPUT_FPGA = 0x4000
TDC_WAITING_FOR_POWER_ON = 0x8000
TDC_WAITING_FOR_FLASH_SETTINGS = 0x10000

# ============================================================
# Image Processing
# ============================================================
REG_IMAGE_CORRECTION = 0xE888  # Command (WO) - trigger NUC
REG_IMAGE_CORRECTION_MODE = 0xE884  # Enum: ImageCorrectionMode
REG_EXTERNAL_BLACKBODY_TEMP = 0xE944  # Float, Celsius (-273.15 to 1500)
REG_BAD_PIXEL_REPLACEMENT = 0xEB60  # Bool (RW)
REG_REVERSE_X = 0xE8D4  # Bool (RW)
REG_REVERSE_Y = 0xE8D8  # Bool (RW)
REG_TEST_IMAGE_SELECTOR = 0xEACC  # Enum: TestImageSelector

# ============================================================
# ROI / Subwindow
# ============================================================
REG_OFFSET_X = 0xEB44  # Int (RW)
REG_OFFSET_Y = 0xEB48  # Int (RW)
REG_CENTER_IMAGE = 0xE8E8  # Bool (RW)

# ============================================================
# Frame Rate (extended)
# ============================================================
REG_FRAME_RATE_MODE = 0xE818  # Enum: FrameRateMode
REG_FRAME_RATE_MAX_FG = 0xE81C  # Float, Hz (RW) - frame grabber limit
REG_FRAME_RATE_MAX = 0xEAB4  # Float, Hz (RO) - camera max
REG_TRIGGER_FRAME_COUNT = 0xEAD0  # Int (RW), 1 to 2^32-1

# ============================================================
# Diagnostics
# ============================================================
REG_DEVICE_TEMPERATURE_SELECTOR = 0xEA6C  # Enum: TemperatureLocation
REG_DEVICE_TEMPERATURE_READOUT = 0xEA70  # Float, Celsius (RO)
REG_DEVICE_VOLTAGE_SELECTOR = 0xEA88  # Enum: VoltageLocation
REG_DEVICE_VOLTAGE_READOUT = 0xEA8C  # Float, Volts (RO)
REG_DEVICE_CURRENT_SELECTOR = 0xEA90  # Enum: CurrentLocation
REG_DEVICE_CURRENT_READOUT = 0xEA94  # Float, Amps (RO)
REG_DEVICE_RUNNING_TIME = 0xEA98  # Int, seconds (RO)
REG_DEVICE_COOLER_RUNNING_TIME = 0xEA9C  # Int, seconds (RO)
REG_DEVICE_POWER_ON_CYCLES = 0xEAA0  # Int, count (RO)
REG_DEVICE_COOLER_POWER_ON_CYCLES = 0xEAA4  # Int, count (RO)

# ============================================================
# Device Management (extended)
# ============================================================
REG_SAVE_CONFIGURATION = 0xEC34  # Command (WO)
REG_LOAD_CONFIG_ON_STARTUP = 0xEC30  # Bool (RW)
REG_ACQ_START_ON_STARTUP = 0xE8E4  # Bool (RW)
REG_POSIX_TIME = 0xE980  # Int, seconds since epoch (RW)
REG_SUB_SECOND_TIME = 0xE984  # Int, 100ns ticks (RO)

# ============================================================
# Calibration Collections
# ============================================================
REG_CAL_COLLECTION_COUNT = 0xE870  # Int (RO)
REG_CAL_COLLECTION_SELECTOR = 0xE874  # Int (RW)
REG_CAL_COLLECTION_POSIX = 0xE878  # Int (RW) - collection timestamp
REG_CAL_COLLECTION_TYPE = 0xEB10  # Int (RO) - enum: 0=TelopsFixed
REG_CAL_COLLECTION_LOAD = 0xE87C  # Command (WO)
REG_CAL_BLOCK_COUNT = 0xEAFC  # Int (RO)
REG_CAL_BLOCK_SELECTOR = 0xEB00  # Int (RW)
REG_CAL_BLOCK_POSIX = 0xEB04  # Int (RW)
REG_CAL_BLOCK_LOAD = 0xEB08  # Command (WO)
REG_CAL_ACTIVE_TYPE = 0xEAE4  # Int (RO)
REG_CAL_ACTIVE_POSIX = 0xE880  # Int (RO)
REG_CAL_ACTIVE_BLOCK_POSIX = 0xEB0C  # Int (RO)

# ============================================================
# GEV Timestamps
# ============================================================
REG_GEV_TIMESTAMP_TICK_FREQ_HIGH = 0x093C  # Int (RO)
REG_GEV_TIMESTAMP_TICK_FREQ_LOW = 0x0940  # Int (RO)
REG_GEV_TIMESTAMP_CONTROL = 0x0944  # Command: 1=reset, 2=latch
REG_GEV_TIMESTAMP_VALUE_HIGH = 0x0948  # Int (RO)
REG_GEV_TIMESTAMP_VALUE_LOW = 0x094C  # Int (RO)

# ============================================================
# Download Speed
# ============================================================
REG_DOWNLOAD_BITRATE_MAX = 0xEAD4  # Float, Mbps (RW, locked when DownloadMode=OFF)

# ============================================================
# Enumerations
# ============================================================


class CalibrationMode(IntEnum):
    """Output calibration applied to each frame (RAW, NUC, radiometric temperature, radiance)."""

    RAW0 = 0
    RAW = 255
    NUC = 1
    RT = 2  # Radiometric temperature (Celsius)
    IBR = 3  # In-band radiance
    IBI = 4  # In-band irradiance


class ExposureAuto(IntEnum):
    """Automatic exposure control mode (off, single-shot, or continuous)."""

    OFF = 0
    ONCE = 1
    CONTINUOUS = 2


class ExposureMode(IntEnum):
    """How the sensor integration time is determined (timed, trigger-width, etc.)."""

    OFF = 0
    TIMED = 1
    TRIGGER_WIDTH = 2
    TRIGGER_CONTROLLED = 3


class AcquisitionMode(IntEnum):
    """Frame acquisition mode (continuous, single, or multi-frame burst)."""

    CONTINUOUS = 0
    SINGLE_FRAME = 1
    MULTI_FRAME = 2


class TriggerSelector(IntEnum):
    """Which trigger function is targeted by TriggerMode/TriggerSource settings."""

    ACQUISITION_START = 0
    FLAGGING = 1
    GATING = 2


class TriggerMode(IntEnum):
    """Enable or disable the selected trigger function."""

    OFF = 0
    ON = 1


class TriggerSource(IntEnum):
    """Signal source for the selected trigger (software command or BNC external input)."""

    SOFTWARE = 0
    EXTERNAL_SIGNAL = 48  # BNC connector


class TriggerActivation(IntEnum):
    """Edge or level polarity that activates the selected trigger."""

    RISING_EDGE = 0
    FALLING_EDGE = 1
    ANY_EDGE = 2
    LEVEL_HIGH = 3
    LEVEL_LOW = 4


class MemoryBufferMode(IntEnum):
    """Enable or disable the onboard 16 GB memory buffer."""

    OFF = 0
    ON = 1


class MemoryBufferStatus(IntEnum):
    """Current operational state of the onboard memory buffer."""

    DEACTIVATED = 0
    IDLE = 1
    HOLDING = 2
    RECORDING = 3
    UPDATING = 4
    TRANSMITTING = 5
    DEFRAGGING = 6
    REFRESH = 255


class MemoryBufferDownloadMode(IntEnum):
    """Unit of download from the onboard buffer (off, full sequence, or single image)."""

    OFF = 0
    SEQUENCE = 1
    IMAGE = 2


class MemoryBufferMOISource(IntEnum):
    """Event source that marks the moment-of-interest (MOI) in a buffer recording."""

    ACQUISITION_STARTED = 0
    SOFTWARE = 1
    EXTERNAL_SIGNAL = 2
    NONE = 255


class MemoryBufferMOIActivation(IntEnum):
    """Edge polarity used to detect the moment-of-interest signal."""

    RISING_EDGE = 0
    FALLING_EDGE = 1
    ANY_EDGE = 2


class IntegrationMode(IntEnum):
    """Timing relationship between sensor integration and readout phases."""

    INTEGRATE_THEN_READ = 0
    INTEGRATE_WHILE_READ = 1


class DetectorMode(IntEnum):
    """Detector operating mode (normal continuous or high-speed burst)."""

    NORMAL = 0
    BURST = 1


class SensorWellDepth(IntEnum):
    """Detector well-depth selection, trading dynamic range for gain."""

    LOW_GAIN = 0
    HIGH_GAIN = 1


class DevicePowerState(IntEnum):
    """Camera power state (standby, fully on, or transitioning between states)."""

    STANDBY = 0
    ON = 1
    IN_TRANSITION = 2


class PixelFormat(IntEnum):
    """GigE Vision pixel format codes for the camera output stream."""

    MONO8 = 0x01080001
    MONO16 = 0x01100007
    MONO10 = 0x01100003
    MONO12 = 0x01100005
    MONO14 = 0x01100025


class ImageCorrectionMode(IntEnum):
    """Reference source used when performing non-uniformity correction (NUC)."""

    BLACK_BODY = 0
    ICU = 1


class TestImageSelector(IntEnum):
    """Built-in test pattern injected instead of live sensor data."""

    OFF = 0
    STATIC_SHADE = 30
    DYNAMIC_SHADE = 31
    CONSTANT_VALUE = 35


class FrameRateMode(IntEnum):
    """Frame rate control strategy (fixed, locked to trigger, maximum, or burst)."""

    FIXED_LOCKED = 0
    FIXED = 1
    MAXIMUM = 2
    BURST = 3


class TemperatureLocation(IntEnum):
    """Selects which internal sensor reports its temperature via the diagnostic register."""

    SENSOR = 0
    MAINBOARD = 1
    INTERNAL_LENS = 2
    EXTERNAL_LENS = 3
    ICU = 4
    FILTER_WHEEL = 5
    COMPRESSOR = 6
    COLD_FINGER = 7
    SPARE = 8
    EXTERNAL_THERMISTOR = 9
    PROCESSING_FPGA = 10
    OUTPUT_FPGA = 11
    STORAGE_FPGA = 12


class VoltageLocation(IntEnum):
    """Selects which power rail is read by the voltage diagnostic register."""

    COOLER = 10
    SUPPLY_24V = 11


class CurrentLocation(IntEnum):
    """Selects which current measurement is read by the current diagnostic register."""

    COOLER = 0
    SUPPLY_24V = 1


class CalibrationCollectionType(IntEnum):
    """Type identifier for a stored calibration collection (currently only Telops-fixed)."""

    TELOPS_FIXED = 0


# ============================================================
# Register metadata: address -> (name, type, access)
# ============================================================
REGISTER_INFO = {
    REG_WIDTH: ("Width", "int", "RW"),
    REG_HEIGHT: ("Height", "int", "RW"),
    REG_PIXEL_FORMAT: ("PixelFormat", "enum", "RW"),
    REG_PAYLOAD_SIZE: ("PayloadSize", "int", "RO"),
    REG_ACQUISITION_MODE: ("AcquisitionMode", "enum", "RW"),
    REG_ACQUISITION_START: ("AcquisitionStart", "cmd", "WO"),
    REG_ACQUISITION_STOP: ("AcquisitionStop", "cmd", "WO"),
    REG_ACQUISITION_ARM: ("AcquisitionArm", "cmd", "WO"),
    REG_EXPOSURE_MODE: ("ExposureMode", "enum", "RW"),
    REG_EXPOSURE_TIME: ("ExposureTime", "float", "RW"),
    REG_ACQUISITION_FRAME_RATE: ("AcquisitionFrameRate", "float", "RW"),
    REG_CALIBRATION_MODE: ("CalibrationMode", "enum", "RW"),
    REG_INTEGRATION_MODE: ("IntegrationMode", "enum", "RW"),
    REG_DETECTOR_MODE: ("DetectorMode", "enum", "RW"),
    REG_SENSOR_WELL_DEPTH: ("SensorWellDepth", "enum", "RW"),
    REG_TRIGGER_SELECTOR: ("TriggerSelector", "enum", "RW"),
    REG_TRIGGER_MODE: ("TriggerMode", "enum", "RW"),
    REG_TRIGGER_SOFTWARE: ("TriggerSoftware", "cmd", "WO"),
    REG_TRIGGER_SOURCE: ("TriggerSource", "enum", "RW"),
    REG_TRIGGER_ACTIVATION: ("TriggerActivation", "enum", "RW"),
    REG_TRIGGER_DELAY: ("TriggerDelay", "float", "RW"),
    REG_MEMORY_BUFFER_MODE: ("MemoryBufferMode", "enum", "RW"),
    REG_MEMORY_BUFFER_SEQ_SIZE: ("MemoryBufferSequenceSize", "int", "RW"),
    REG_MEMORY_BUFFER_DOWNLOAD_MODE: ("MemoryBufferDownloadMode", "enum", "RW"),
    REG_MEMORY_BUFFER_STATUS: ("MemoryBufferStatus", "enum", "RO"),
    REG_DEVICE_POWER_STATE_SETPOINT: ("DevicePowerStateSetpoint", "enum", "RW"),
    REG_DEVICE_POWER_STATE: ("DevicePowerState", "enum", "RO"),
    REG_DEVICE_NOT_READY: ("DeviceNotReady", "bool", "RO"),
    REG_TDC_STATUS: ("TDCStatus", "int", "RO"),
    REG_DEVICE_TEMPERATURE: ("DeviceTemperature", "float", "RO"),
    REG_DEVICE_RESET: ("DeviceReset", "cmd", "WO"),
    # Image Processing
    REG_IMAGE_CORRECTION: ("ImageCorrection", "cmd", "WO"),
    REG_IMAGE_CORRECTION_MODE: ("ImageCorrectionMode", "enum", "RW"),
    REG_EXTERNAL_BLACKBODY_TEMP: ("ExternalBlackbodyTemp", "float", "RW"),
    REG_BAD_PIXEL_REPLACEMENT: ("BadPixelReplacement", "bool", "RW"),
    REG_REVERSE_X: ("ReverseX", "bool", "RW"),
    REG_REVERSE_Y: ("ReverseY", "bool", "RW"),
    REG_TEST_IMAGE_SELECTOR: ("TestImageSelector", "enum", "RW"),
    # ROI / Subwindow
    REG_OFFSET_X: ("OffsetX", "int", "RW"),
    REG_OFFSET_Y: ("OffsetY", "int", "RW"),
    REG_CENTER_IMAGE: ("CenterImage", "bool", "RW"),
    # Frame Rate (extended)
    REG_FRAME_RATE_MODE: ("FrameRateMode", "enum", "RW"),
    REG_FRAME_RATE_MAX_FG: ("FrameRateMaxFG", "float", "RW"),
    REG_FRAME_RATE_MAX: ("FrameRateMax", "float", "RO"),
    REG_TRIGGER_FRAME_COUNT: ("TriggerFrameCount", "int", "RW"),
    # Diagnostics
    REG_DEVICE_TEMPERATURE_SELECTOR: ("DeviceTemperatureSelector", "enum", "RW"),
    REG_DEVICE_TEMPERATURE_READOUT: ("DeviceTemperatureReadout", "float", "RO"),
    REG_DEVICE_VOLTAGE_SELECTOR: ("DeviceVoltageSelector", "enum", "RW"),
    REG_DEVICE_VOLTAGE_READOUT: ("DeviceVoltageReadout", "float", "RO"),
    REG_DEVICE_CURRENT_SELECTOR: ("DeviceCurrentSelector", "enum", "RW"),
    REG_DEVICE_CURRENT_READOUT: ("DeviceCurrentReadout", "float", "RO"),
    REG_DEVICE_RUNNING_TIME: ("DeviceRunningTime", "int", "RO"),
    REG_DEVICE_COOLER_RUNNING_TIME: ("DeviceCoolerRunningTime", "int", "RO"),
    REG_DEVICE_POWER_ON_CYCLES: ("DevicePowerOnCycles", "int", "RO"),
    REG_DEVICE_COOLER_POWER_ON_CYCLES: ("DeviceCoolerPowerOnCycles", "int", "RO"),
    # Device Management (extended)
    REG_SAVE_CONFIGURATION: ("SaveConfiguration", "cmd", "WO"),
    REG_LOAD_CONFIG_ON_STARTUP: ("LoadConfigOnStartup", "bool", "RW"),
    REG_ACQ_START_ON_STARTUP: ("AcqStartOnStartup", "bool", "RW"),
    REG_POSIX_TIME: ("PosixTime", "int", "RW"),
    REG_SUB_SECOND_TIME: ("SubSecondTime", "int", "RO"),
    # GEV Timestamps
    REG_GEV_TIMESTAMP_TICK_FREQ_HIGH: ("GevTimestampTickFreqHigh", "int", "RO"),
    REG_GEV_TIMESTAMP_TICK_FREQ_LOW: ("GevTimestampTickFreqLow", "int", "RO"),
    REG_GEV_TIMESTAMP_CONTROL: ("GevTimestampControl", "cmd", "WO"),
    REG_GEV_TIMESTAMP_VALUE_HIGH: ("GevTimestampValueHigh", "int", "RO"),
    REG_GEV_TIMESTAMP_VALUE_LOW: ("GevTimestampValueLow", "int", "RO"),
    # Download Speed
    REG_DOWNLOAD_BITRATE_MAX: ("DownloadBitrateMax", "float", "RW"),
    # Calibration Collections
    REG_CAL_COLLECTION_COUNT: ("CalCollectionCount", "int", "RO"),
    REG_CAL_COLLECTION_SELECTOR: ("CalCollectionSelector", "int", "RW"),
    REG_CAL_COLLECTION_POSIX: ("CalCollectionPosix", "int", "RW"),
    REG_CAL_COLLECTION_TYPE: ("CalCollectionType", "enum", "RO"),
    REG_CAL_COLLECTION_LOAD: ("CalCollectionLoad", "cmd", "WO"),
    REG_CAL_BLOCK_COUNT: ("CalBlockCount", "int", "RO"),
    REG_CAL_BLOCK_SELECTOR: ("CalBlockSelector", "int", "RW"),
    REG_CAL_BLOCK_POSIX: ("CalBlockPosix", "int", "RW"),
    REG_CAL_BLOCK_LOAD: ("CalBlockLoad", "cmd", "WO"),
    REG_CAL_ACTIVE_TYPE: ("CalActiveType", "int", "RO"),
    REG_CAL_ACTIVE_POSIX: ("CalActivePosix", "int", "RO"),
    REG_CAL_ACTIVE_BLOCK_POSIX: ("CalActiveBlockPosix", "int", "RO"),
}

FEATURE_TO_ADDRESS = {v[0]: k for k, v in REGISTER_INFO.items()}
