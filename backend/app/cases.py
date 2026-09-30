"""Load trusted, operator-installed cases without executing their code."""
import ast
import random
import re
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class CaseFormatError(ValueError):
    pass


class SpecModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


NonEmpty = Annotated[str, Field(min_length=1, pattern=r"\S")]


class ServiceSpec(SpecModel):
    name: NonEmpty
    port: Annotated[int, Field(ge=1, le=65535)]
    protocol: Literal["tcp", "udp"]


class RedSpec(SpecModel):
    objective_path: str

    @field_validator("objective_path")
    @classmethod
    def absolute_path(cls, value):
        path = PurePosixPath(value)
        if not path.is_absolute() or path == PurePosixPath("/") or ".." in path.parts:
            raise ValueError("objective_path must be an absolute file path without '..'")
        return value


class BlueSpec(SpecModel):
    stabilization_seconds: Annotated[int, Field(gt=0)]


class CheckSpec(SpecModel):
    health: NonEmpty
    exploit: NonEmpty


class CaseSpec(SpecModel):
    id: Annotated[str, Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]
    name: NonEmpty
    category: NonEmpty
    difficulty: NonEmpty
    required_services: Annotated[list[ServiceSpec], Field(min_length=1)]
    red: RedSpec
    blue: BlueSpec
    checks: CheckSpec

    @field_validator("required_services")
    @classmethod
    def unique_services(cls, services):
        if len({s.name for s in services}) != len(services):
            raise ValueError("service names must be unique")
        if len({(s.port, s.protocol) for s in services}) != len(services):
            raise ValueError("service endpoints must be unique")
        return services


class CaseValidator:
    def validate(self, data, directory: Path) -> CaseSpec:
        spec = CaseSpec.model_validate(data)
        if spec.id != directory.name:
            raise CaseFormatError("id must match the case directory name")
        root = directory.resolve()

        def contained(relative):
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts:
                raise CaseFormatError(f"unsafe case path: {relative}")
            result = (root / path).resolve()
            if not result.is_relative_to(root):
                raise CaseFormatError(f"path escapes case directory: {relative}")
            return result

        if not contained("rootfs").is_dir():
            raise CaseFormatError("rootfs directory is missing")
        dockerfile = contained("Dockerfile").read_text()
        bases = re.findall(r"^\s*FROM\s+(.+?)\s*$", dockerfile, re.I | re.M)
        if not bases or any(base.lower() != "ubuntu:20.04" for base in bases):
            raise CaseFormatError("every FROM must be exactly ubuntu:20.04")
        for name in (spec.checks.health, spec.checks.exploit):
            path = contained(name)
            if path.suffix != ".py" or not path.is_file():
                raise CaseFormatError(f"missing Python checker: {name}")
            ast.parse(path.read_text(), filename=name)
        return spec


class CaseLoader:
    def load(self, directory: Path) -> CaseSpec:
        directory = Path(directory)
        try:
            manifest = directory / "case.yaml"
            if not manifest.resolve().is_relative_to(directory.resolve()):
                raise CaseFormatError("case.yaml escapes case directory")
            data = yaml.safe_load(manifest.read_text())
            return CaseValidator().validate(data, directory)
        except (OSError, UnicodeError, yaml.YAMLError, ValidationError, SyntaxError) as exc:
            raise CaseFormatError(str(exc)) from exc


class CaseCatalog:
    def __init__(self, directory: Path = Path("cases")):
        directory = Path(directory)
        self._cases: dict[str, CaseSpec] = {}
        self.invalid: dict[str, str] = {}
        loader = CaseLoader()
        # Sort before random selection so directory enumeration cannot affect the seed.
        for path in sorted(directory.iterdir()):
            if path.name.startswith(".") or not path.is_dir():
                continue
            try:
                if path.is_symlink():
                    raise CaseFormatError("case directories must not be symlinks")
                self._cases[path.name] = loader.load(path)
            except CaseFormatError as exc:
                self.invalid[path.name] = str(exc)

    def list(self) -> list[CaseSpec]:
        return list(self._cases.values())

    def get(self, id: str) -> CaseSpec:
        return self._cases[id]

    def random(self, seed: int | str) -> CaseSpec:
        if not self._cases:
            raise CaseFormatError("no READY cases")
        return random.Random(seed).choice(self.list())
