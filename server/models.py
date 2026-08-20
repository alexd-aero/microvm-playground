"""Request/response schemas."""
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from . import config as C

TTL_CHOICES = {"never": 0, "15m": 900, "1h": 3600, "4h": 14400, "12h": 43200}


class CreateVM(BaseModel):
    name: str = Field(default="", max_length=32)
    vcpus: int = Field(default=C.DEFAULT_VCPUS, ge=C.VCPU_MIN, le=C.VCPU_MAX)
    mem_mib: int = Field(default=C.DEFAULT_MEM_MIB, ge=C.MEM_MIN_MIB, le=C.MEM_MAX_MIB)
    disk_gb: int = Field(default=C.DEFAULT_DISK_GB, ge=C.DISK_MIN_GB, le=C.DISK_MAX_GB)
    ttl: Literal["never", "15m", "1h", "4h", "12h"] = "1h"

    @field_validator("name")
    @classmethod
    def _clean_name(cls, v: str) -> str:
        v = "".join(ch for ch in v.strip() if ch.isalnum() or ch in "-_").lower()
        return v[:32]


class VMView(BaseModel):
    id: str
    name: str
    state: str
    vcpus: int
    mem_mib: int
    disk_gb: int
    ip: Optional[str] = None
    gateway: Optional[str] = None
    created_at: float
    boot_ms: Optional[int] = None
    expires_at: Optional[float] = None
    error: Optional[str] = None


class HostInfo(BaseModel):
    mode: str                          # firecracker | qemu | mock
    kvm: bool
    firecracker: Optional[str] = None
    qemu: Optional[str] = None
    accel: Optional[str] = None        # kvm | whpx | hvf | tcg
    kernel: bool
    rootfs: bool
    image: bool = False
    root: bool
    max_vms: int
    limits: dict
    defaults: dict
    problems: list[str] = []
    notes: list[str] = []
