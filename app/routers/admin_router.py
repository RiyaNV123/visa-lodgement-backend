from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import require_admin
from app.models import User
from app.sheet_store import StoreError, as_int, case_rows, courses_for_case, rows

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/students")
def list_students(current_user: User = Depends(require_admin)):
    """Every signed-up student, with their case summary if they've started one."""
    try:
        students = [item for item in rows("Users") if item["role"] == "student"]
        cases_by_owner = {as_int(item["owner_user_id"]): item for item in case_rows()}

        result = []
        for student in students:
            student_id = as_int(student["id"])
            case = cases_by_owner.get(student_id)
            case_summary = None
            if case:
                case_id = as_int(case["id"])
                case_summary = {
                    "id": case_id,
                    "stream": case["stream"],
                    "status": case["status"],
                    "course_count": len(courses_for_case(case_id)),
                    "created_at": case["created_at"],
                }
            result.append(
                {
                    "id": student_id,
                    "email": student["email"],
                    "full_name": student["full_name"],
                    "created_at": student["created_at"],
                    "case": case_summary,
                }
            )
        result.sort(key=lambda item: item["created_at"], reverse=True)
        return result
    except StoreError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Google Sheets is unavailable: {exc}") from exc
