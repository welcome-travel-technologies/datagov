import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import LoginPage from "./page";

const mocks = vi.hoisted(() => ({
  login: vi.fn(),
  push: vi.fn(),
  search: new URLSearchParams(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mocks.push }),
  useSearchParams: () => mocks.search,
}));
vi.mock("@/lib/auth", () => ({ useAuth: () => ({ login: mocks.login }) }));
vi.mock("@/lib/branding", () => ({
  useBranding: () => ({ name: "Welcome" }),
  BrandLogo: () => null,
}));

const browserWindow = window;
const navigate = vi.fn();
const fetchMock = vi.fn<typeof fetch>();
const googleUrl = "https://accounts.google.com/o/oauth2/v2/auth?state=test";
let queryClient: QueryClient;

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderLogin() {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <LoginPage />
    </QueryClientProvider>,
  );
}

async function googleButton() {
  const button = screen.getByRole("button", { name: "Continue with Google" });
  await waitFor(() => expect(button).toBeEnabled());
  return button;
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.search = new URLSearchParams();
  mocks.login.mockResolvedValue(undefined);
  fetchMock.mockImplementation(async (input) => {
    if (input === "/api/auth/google/config/") return jsonResponse({ enabled: true });
    if (input === "/api/auth/google/start/") return jsonResponse({ url: googleUrl });
    throw new Error(`Unexpected request: ${String(input)}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("window", new Proxy(browserWindow, {
    get(target, property) {
      if (property === "location") return { assign: navigate };
      return Reflect.get(target, property, target);
    },
  }));
  document.cookie = "csrftoken=login-csrf; path=/";
});

afterEach(() => {
  cleanup();
  queryClient?.clear();
  document.cookie = "csrftoken=; max-age=0; path=/";
  vi.unstubAllGlobals();
});

describe("Google sign-in and sign-up", () => {
  it("starts sign-in without password fields and sends the session CSRF token", async () => {
    mocks.search.set("next", "/api/o/authorize/?client_id=example");
    renderLogin();
    const button = await googleButton();
    expect(screen.getByText(/automatically creates your account with basic access/)).toBeVisible();

    fireEvent.click(button);

    await waitFor(() => expect(navigate).toHaveBeenCalledWith(googleUrl));
    expect(fetchMock).toHaveBeenCalledWith("/api/auth/google/start/", expect.objectContaining({
      method: "POST",
      credentials: "include",
      headers: expect.objectContaining({ "X-CSRFToken": "login-csrf" }),
      body: JSON.stringify({ next: "/api/o/authorize/?client_id=example" }),
    }));
    expect(mocks.login).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Connecting to Google..." })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Sign In" })).toBeDisabled();
  });

  it("keeps Google disabled while config loads and leaves password login available", () => {
    fetchMock.mockReturnValue(new Promise(() => {}));
    renderLogin();
    expect(screen.getByRole("button", { name: "Continue with Google" })).toBeDisabled();
    expect(screen.getByText("Checking Google sign-in...")).toBeVisible();
    expect(screen.getByRole("button", { name: "Sign In" })).toBeEnabled();
  });

  it.each(["disabled", "unreachable"])("shows unavailable Google sign-in when config is %s", async (state) => {
    if (state === "disabled") fetchMock.mockResolvedValue(jsonResponse({ enabled: false }));
    else fetchMock.mockRejectedValue(new Error("Network unavailable"));
    renderLogin();

    expect(await screen.findByText("Google sign-in is currently unavailable.")).toBeVisible();
    const button = screen.getByRole("button", { name: "Continue with Google" });
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(fetchMock).not.toHaveBeenCalledWith("/api/auth/google/start/", expect.anything());
    expect(screen.getByRole("button", { name: "Sign In" })).toBeEnabled();
  });

  it("shows a start failure and allows another attempt", async () => {
    fetchMock.mockImplementation(async (input) => input === "/api/auth/google/config/"
      ? jsonResponse({ enabled: true })
      : jsonResponse({ detail: "Google sign-in is temporarily unavailable." }, 503));
    renderLogin();
    fireEvent.click(await googleButton());

    expect(await screen.findByRole("alert")).toHaveTextContent("Google sign-in is temporarily unavailable.");
    expect(await googleButton()).toBeEnabled();
    expect(navigate).not.toHaveBeenCalled();
  });

  it.each([
    ["cancelled", "Google sign-in was cancelled."],
    ["expired", "Your Google sign-in session expired."],
    ["unavailable", "Google sign-in is currently unavailable."],
    ["failed", "Google sign-in failed."],
    ["account_conflict", "This Google account could not be linked."],
    ["inactive", "Your account is inactive."],
    ["<script>untrusted error</script>", "Google sign-in failed."],
    ["__proto__", "Google sign-in failed."],
    ["toString", "Google sign-in failed."],
  ])("explains the %s callback error", async (code, message) => {
    mocks.search.set("google_error", code);
    renderLogin();
    expect(screen.getByRole("alert")).toHaveTextContent(message);
    await googleButton();
  });

  it.each([
    "https://outside.example/",
    "//outside.example/",
    "/\\outside.example/",
    "/\n/outside.example/",
    "/dashboard\u0000",
  ])("replaces the unsafe return path %j before starting Google sign-in", async (next) => {
    mocks.search.set("next", next);
    renderLogin();
    fireEvent.click(await googleButton());
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/api/auth/google/start/",
      expect.objectContaining({ body: JSON.stringify({ next: "/dashboard" }) }),
    ));
  });
});

describe("adding Google sign-in to an existing account", () => {
  const accountEmail = "jane@example.com";

  async function confirmAccount(password = "existing-password") {
    const button = await screen.findByRole("button", { name: "Add Google sign-in" });
    fireEvent.change(screen.getByLabelText("Existing password"), { target: { value: password } });
    fireEvent.click(button);
  }

  it("uses server-confirmed matching email and posts only the existing password with CSRF", async () => {
    const destination = "/api/o/authorize/?client_id=existing-client";
    fetchMock.mockImplementation(async (input) => {
      if (input === "/api/auth/google/config/") {
        return jsonResponse({ enabled: true, pending_link: { email: accountEmail } });
      }
      if (input === "/api/auth/google/link/") return jsonResponse({ url: destination });
      throw new Error(`Unexpected request: ${String(input)}`);
    });
    renderLogin();

    const email = await screen.findByLabelText("Account email");
    expect(email).toHaveValue(accountEmail);
    expect(email).toHaveAttribute("readonly");
    expect(screen.queryByLabelText("Email or Username")).not.toBeInTheDocument();
    expect(screen.getByText(/Enter your existing password once/)).toHaveTextContent("Your password will continue to work.");
    await confirmAccount();

    await waitFor(() => expect(navigate).toHaveBeenCalledWith(destination));
    expect(fetchMock).toHaveBeenCalledWith("/api/auth/google/link/", expect.objectContaining({
      method: "POST",
      credentials: "include",
      headers: expect.objectContaining({ "X-CSRFToken": "login-csrf" }),
      body: JSON.stringify({ password: "existing-password" }),
    }));
    expect(mocks.login).not.toHaveBeenCalled();
    expect(mocks.push).not.toHaveBeenCalled();
  });

  it("does not enable account confirmation from query parameters alone", async () => {
    mocks.search.set("google_link", "1");
    mocks.search.set("email", "attacker@example.com");
    renderLogin();
    await googleButton();

    expect(screen.queryByRole("button", { name: "Add Google sign-in" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Account email")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Email or Username")).toHaveValue("");
    expect(screen.getByRole("button", { name: "Sign In" })).toBeEnabled();
    expect(fetchMock).not.toHaveBeenCalledWith("/api/auth/google/link/", expect.anything());
  });

  it("keeps pending confirmation after a wrong password and permits a retry", async () => {
    fetchMock.mockImplementation(async (input) => input === "/api/auth/google/config/"
      ? jsonResponse({ enabled: true, pending_link: { email: accountEmail } })
      : jsonResponse({ error: "The existing password was incorrect.", code: "failed" }, 400));
    renderLogin();
    await confirmAccount("incorrect-password");

    expect(await screen.findByRole("alert")).toHaveTextContent("The existing password was incorrect.");
    const button = screen.getByRole("button", { name: "Add Google sign-in" });
    await waitFor(() => expect(button).toBeEnabled());
    expect(screen.getByLabelText("Existing password")).toHaveValue("");
    expect(screen.getByLabelText("Account email")).toHaveValue(accountEmail);
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/auth/google/config/")).toHaveLength(2);
    expect(mocks.login).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
  });

  it.each([
    [400, "This account confirmation expired. Please start Google sign-in again.", "expired"],
    [409, "This Google account could not be linked.", "account_conflict"],
  ])("refreshes a cleared pending link after status %s and preserves the specific error", async (status, error, code) => {
    let pending = true;
    fetchMock.mockImplementation(async (input) => {
      if (input === "/api/auth/google/config/") {
        return jsonResponse({ enabled: true, pending_link: pending ? { email: accountEmail } : null });
      }
      pending = false;
      return jsonResponse({ error, code }, status as number);
    });
    renderLogin();
    await confirmAccount();

    await screen.findByRole("button", { name: "Sign In" });
    expect(screen.queryByLabelText("Account email")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toHaveValue("");
    expect(screen.getByRole("alert")).toHaveTextContent(error as string);
    expect(mocks.login).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
  });

  it("can restart with another Google account from the confirmation screen", async () => {
    fetchMock.mockImplementation(async (input) => input === "/api/auth/google/config/"
      ? jsonResponse({ enabled: true, pending_link: { email: accountEmail } })
      : jsonResponse({ url: googleUrl }));
    renderLogin();
    fireEvent.click(await screen.findByRole("button", { name: "Use another Google account" }));

    await waitFor(() => expect(navigate).toHaveBeenCalledWith(googleUrl));
    expect(fetchMock).toHaveBeenCalledWith("/api/auth/google/start/", expect.objectContaining({ method: "POST" }));
    expect(fetchMock).not.toHaveBeenCalledWith("/api/auth/google/link/", expect.anything());
    expect(mocks.login).not.toHaveBeenCalled();
  });
});

describe("password sign-in", () => {
  async function submitPassword() {
    fireEvent.change(screen.getByLabelText("Email or Username"), { target: { value: "jane" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "test-password" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign In" }));
    await waitFor(() => expect(mocks.login).toHaveBeenCalledWith("jane", "test-password"));
  }

  it.each([
    ["/dictionary", "/dictionary"],
    ["/\\outside.example/", "/dashboard"],
  ])("keeps password login working with next=%s", async (next, destination) => {
    mocks.search.set("next", next);
    renderLogin();
    await submitPassword();
    await waitFor(() => expect(mocks.push).toHaveBeenCalledWith(destination));
    expect(navigate).not.toHaveBeenCalled();
  });

  it("uses browser navigation for Django OAuth authorization after password login", async () => {
    const next = "/api/o/authorize/?client_id=example";
    mocks.search.set("next", next);
    renderLogin();
    await submitPassword();
    await waitFor(() => expect(navigate).toHaveBeenCalledWith(next));
    expect(mocks.push).not.toHaveBeenCalled();
  });

  it("shows credential errors and lets the user retry", async () => {
    mocks.login.mockRejectedValue(new Error("Your credentials did not match."));
    renderLogin();
    await submitPassword();
    expect(await screen.findByRole("alert")).toHaveTextContent("Your credentials did not match.");
    expect(screen.getByRole("button", { name: "Sign In" })).toBeEnabled();
    expect(mocks.push).not.toHaveBeenCalled();
  });
});
