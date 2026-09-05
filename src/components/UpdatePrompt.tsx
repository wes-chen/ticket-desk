/**
 * "A new version is available - reload" prompt. ops#18.
 *
 * The service worker precaches everything so the app works offline, which matters at
 * the arena. The cost is that a deploy is not visible until the worker is replaced.
 *
 * Three options were on the table in ops#18. `skipWaiting` + `clientsClaim` activates
 * immediately but can swap assets mid-session. Silent `autoUpdate` - the previous
 * behaviour - shows the old version on the first load after a deploy, which is exactly
 * the "quietly out of date" failure the issue is about. This is the third: tell the
 * user, and let them choose the moment.
 *
 * Deliberately not auto-reloading. A reload discards anything half-typed, and the list
 * price fields in this app are typed by hand.
 */

import { useRegisterSW } from "virtual:pwa-register/react";

export default function UpdatePrompt() {
  const {
    needRefresh: [needRefresh, setNeedRefresh],
    updateServiceWorker,
  } = useRegisterSW({
    onRegisteredSW(_url, registration) {
      // Check hourly while the tab is open. The dangerous case is a phone left open on
      // this page for days and then consulted at a deadline; without a periodic check
      // the worker only looks for an update on navigation.
      if (registration) {
        setInterval(() => void registration.update(), 60 * 60 * 1000);
      }
    },
  });

  /**
   * The button must do what it says.
   *
   * `updateServiceWorker(true)` sends SKIP_WAITING and reloads on `controllerchange` -
   * but only if there IS a waiting worker. Measured while testing this: the prompt can
   * still be showing after the new worker has already activated on its own, at which
   * point there is nothing to skip, no controllerchange fires, and clicking Reload
   * produced zero navigations. A banner that does nothing when clicked is worse than no
   * banner.
   *
   * So reload unconditionally afterwards. If updateServiceWorker did navigate, this is a
   * no-op on an already-unloading page.
   */
  const reload = async () => {
    try {
      await updateServiceWorker(true);
    } catch {
      // An update failure must not strand the user on a stale page.
    }
    location.reload();
  };

  if (!needRefresh) return null;

  return (
    <div className="fixed inset-x-0 bottom-0 z-50 border-t border-teal-600 bg-teal-800 px-4 py-3 text-sm text-white shadow-lg">
      <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-3">
        <span>
          <strong>A new version is available.</strong> The page you are looking at was built
          earlier &mdash; reload to get current deadlines and market data.
        </span>
        <span className="flex gap-2">
          <button
            onClick={() => void reload()}
            className="rounded bg-white px-3 py-1.5 font-medium text-teal-900 hover:bg-teal-50"
          >
            Reload
          </button>
          <button
            onClick={() => setNeedRefresh(false)}
            className="rounded border border-teal-400 px-3 py-1.5 hover:bg-teal-700"
          >
            Later
          </button>
        </span>
      </div>
    </div>
  );
}
