import { Navigate, Route, Routes } from "react-router-dom";

import { useAuth } from "./authContext";
import { Layout } from "./components";
import { AlertsPage } from "./pages/Alerts";
import { CategoriesPage } from "./pages/Categories";
import { OverviewPage } from "./pages/Overview";
import { SessionDetailPage } from "./pages/SessionDetail";
import { SessionsPage } from "./pages/Sessions";
import { SignInPage } from "./pages/SignIn";
import { UsersPage } from "./pages/Users";

export function App() {
  const { adminKey } = useAuth();
  if (!adminKey) return <SignInPage />;
  return (
    <Routes>
      <Route element={<Layout />}>
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
