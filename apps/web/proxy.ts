import { NextResponse, type NextRequest } from "next/server";

// Optional gate for the whole site: set WEB_BASIC_AUTH="user:password" in the
// deployment environment. Unset, the site is public (it shows demo data only).
export function proxy(req: NextRequest) {
  const expected = process.env.WEB_BASIC_AUTH;
  if (!expected) return NextResponse.next();
  const header = req.headers.get("authorization") ?? "";
  const [scheme, encoded] = header.split(" ");
  if (scheme === "Basic" && encoded && atob(encoded) === expected) return NextResponse.next();
  return new NextResponse("Authentication required", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="Project Brain"' },
  });
}

export const config = { matcher: ["/((?!_next/static|_next/image|fonts/|icon.svg).*)"] };
