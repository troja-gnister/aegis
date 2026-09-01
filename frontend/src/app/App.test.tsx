import {render, screen} from "@testing-library/react";
import {App} from "./App";
import "../styles/global.css";

test("renders the accessible dark application shell", () => {
  render(<App />);
  expect(screen.getByRole("banner")).toHaveTextContent("Aegis");
  expect(screen.getByRole("main")).toHaveTextContent("Secure file access");
  expect(document.documentElement.dataset.theme).toBe("dark");
});

test("gives the Aegis home link a touch-sized inline target", () => {
  render(<App />);

  const brand = screen.getByRole("link", {name: "Aegis home"});
  expect(getComputedStyle(brand).minWidth).toBe("var(--touch-target)");
});
