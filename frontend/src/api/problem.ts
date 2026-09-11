export const GENERIC_PROBLEM_TYPE = "request_failed";
export const GENERIC_PROBLEM_TITLE = "Request failed";

export class ApiProblem extends Error {
  readonly type: string;
  readonly title: string;
  readonly status: number;
  readonly retryAfterSeconds?: number;

  constructor({
    type,
    title,
    status,
    retryAfterSeconds,
  }: {
    type: string;
    title: string;
    status: number;
    retryAfterSeconds?: number;
  }) {
    super(title);
    this.name = "ApiProblem";
    this.type = type;
    this.title = title;
    this.status = status;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

export function genericApiProblem(status = 0): ApiProblem {
  return new ApiProblem({
    type: GENERIC_PROBLEM_TYPE,
    title: GENERIC_PROBLEM_TITLE,
    status,
  });
}
