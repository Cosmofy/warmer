from enum import Enum

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class Code(Enum):
    UNAUTHORIZED = (401, "A valid Warmer administration token is required.")
    INVALID_REQUEST = (422, "The request is invalid.")
    UNKNOWN_QUERY = (404, "The requested warming operation is not registered.")
    RUN_NOT_FOUND = (404, "The requested run was not found.")
    RUN_IN_PROGRESS = (409, "Another discovery or warming run is already in progress.")
    DISCOVERY_REQUIRED = (409, "Refresh the POP inventory before starting a warming run.")
    INTERNAL_ERROR = (500, "An unexpected error occurred.")
    SHUTTING_DOWN = (503, "The service is shutting down; retry after it restarts.")

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message


class Error(Exception):
    def __init__(self, code: Code):
        self.code = code
        super().__init__(code.name)


async def handle_error(_request: Request, error: Error) -> JSONResponse:
    return JSONResponse(status_code=error.code.status,
                        headers={"WWW-Authenticate": "Bearer"} if error.code == Code.UNAUTHORIZED else None,
                        content={"error": {"code": error.code.name, "message": error.code.message}})


async def handle_validation_error(request: Request, _error: RequestValidationError) -> JSONResponse:
    return await handle_error(request, Error(Code.INVALID_REQUEST))


async def handle_unexpected_error(request: Request, _error: Exception) -> JSONResponse:
    response = await handle_error(request, Error(Code.INTERNAL_ERROR))
    if request_id := getattr(request.state, "request_id", None):
        response.headers["x-request-id"] = request_id
    return response
