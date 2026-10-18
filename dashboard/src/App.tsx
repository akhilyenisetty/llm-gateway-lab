import { Navigate, Route, Routes } from "react-router-dom";

import { useAuth } from "./authContext";
import { Layout } from "./components";
import { AlertsPage } from "./pages/Alerts";
import { CategoriesPage } from "./pages/Categories";
import { MyUsagePage } from "./pages/MyUsage";
import { OverviewPage } from "./pages/Overview";
import { MySessionPage, SessionDetailPage } from "./pages/SessionDetail";
import { SessionsPage } from "./pages/Sessions";
import { SignInPage } from "./pages/SignIn";
import { UsersPage } from "./pages/Users";

export function App() {
  const { apiKey, role } = useAuth();
  if (!apiKey || !role) return <SignInPage />;
  if (role === "user") {
    // A gateway key: only its own usage. Admin pages don't exist in this view (and the
    // admin API would refuse this key anyway).
    return (
      <Routes>
        <Route element={<Layout role="user" />}>
          <Route index element={<MyUsagePage />} />
          <Route path="sessions/:sessionId" element={<MySessionPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    );
  }
  return (
    <Routes>
      <Route element={<Layout role="admin" />}>
        <Route index element={<OverviewPage />} />
        <Route path="categories" element={<CategoriesPage />} />
        <Route path="users" element={<UsersPage />} />
        <Route path="sessions" element={<SessionsPage />} />
        <Route path="sessions/:keyId/:sessionId" element={<SessionDetailPage />} />
        <Route path="alerts" element={<AlertsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
