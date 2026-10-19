export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  /** Name of the form field the error belongs to, if any. */
  readonly field?: string;

  constructor(status: number, code: string, message: string, field?: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.field = field;
  }
}
