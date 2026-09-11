"""Shared values for the Google Sheets-backed application store."""

import enum
from dataclasses import dataclass


class UserRole(str, enum.Enum):
    student = "student"
    admin = "admin"


class Stream(str, enum.Enum):
    vocational = "vocational"
    higher = "higher"


class CourseType(str, enum.Enum):
    certificate = "certificate"
    diploma = "diploma"
    bachelors = "bachelors"
    masters = "masters"


class DocType(str, enum.Enum):
    coe = "coe"
    completion_letter = "completion_letter"
    transcript = "transcript"
    academic_certificate = "academic_certificate"
    current_visa = "current_visa"
    afp_certificate = "afp_certificate"
    afp_receipt = "afp_receipt"
    pte = "pte"
    ovhc = "ovhc"
    new_coe = "new_coe"


CASE_DOC_TYPES = {DocType.current_visa, DocType.afp_certificate, DocType.afp_receipt, DocType.pte, DocType.ovhc, DocType.new_coe}


@dataclass
class User:
    id: int
    email: str
    hashed_password: str
    full_name: str
    role: UserRole
