import {test as base, type TestInfoError} from "@playwright/test";

// Sanitization happens before context teardown writes automatic error context,
// not merely before the reporter prints it. Keep credentials out of both paths.
export const test = base.extend({
  page: async ({page}, use, testInfo) => {
    try {
      await use(page);
    } finally {
      for (const error of testInfo.errors) {
        for (const key of Object.keys(error)) delete error[key as keyof TestInfoError];
        error.message = "Browser check failed; free-text diagnostics withheld.";
      }
      if (!page.isClosed()) await page.locator("input, textarea").evaluateAll((fields) => {
        for (const field of fields) (field as HTMLInputElement).value = "";
      });
      if (testInfo.status !== testInfo.expectedStatus && !page.isClosed()) {
        await page.screenshot({
          path: testInfo.outputPath("sanitized.png"),
          mask: [page.locator("input, textarea")],
          fullPage: false,
        });
      }
    }
  },
});
