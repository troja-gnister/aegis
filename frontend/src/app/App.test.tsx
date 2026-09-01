import {render, screen} from "@testing-library/react";
import {App} from "./App";

test("renders the accessible dark application shell", () => {
  render(<App />);
  expect(screen.getByRole("banner")).toHaveTextContent("Aegis");
  expect(screen.getByRole("main")).toHaveTextContent("Secure file access");
  expect(document.documentElement.dataset.theme).toBe("dark");
});
