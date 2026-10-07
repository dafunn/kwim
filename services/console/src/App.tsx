import { Navigate, Route, Routes } from "react-router-dom";
import { NavigationSync } from "./auth/NavigationSync";
import { AppShell } from "./components/AppShell";
import { Login } from "./routes/Login";
import { TeamsOverview } from "./routes/TeamsOverview";
import { AllKeys } from "./routes/console/AllKeys";
import { AuditLog } from "./routes/console/AuditLog";
import { ConsoleLayout } from "./routes/console/ConsoleLayout";
import { Jobs } from "./routes/console/Jobs";
import { Operators } from "./routes/console/Operators";
import { CommitLog } from "./routes/team/CommitLog";
import { Code } from "./routes/team/Code";
import { Keys } from "./routes/team/Keys";
import { Knowledge } from "./routes/team/Knowledge";
import { Memory } from "./routes/team/Memory";
import { MemoryEpisodic } from "./routes/team/MemoryEpisodic";
import { MemorySemantic } from "./routes/team/MemorySemantic";
import { MemoryWorking } from "./routes/team/MemoryWorking";
import { Proposals } from "./routes/team/Proposals";
import { TeamDetailLayout } from "./routes/team/TeamDetailLayout";
import { Wisdom } from "./routes/team/Wisdom";

export function App() {
  return (
    <>
      <NavigationSync />
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route element={<AppShell />}>
          <Route path="/teams" element={<TeamsOverview />} />
          <Route path="/teams/:team" element={<TeamDetailLayout />}>
            <Route index element={<Navigate to="knowledge" replace />} />
            <Route path="knowledge" element={<Knowledge />} />
            <Route path="wisdom" element={<Wisdom />} />
            <Route path="memory" element={<Memory />}>
              <Route index element={<Navigate to="semantic" replace />} />
              <Route path="semantic" element={<MemorySemantic />} />
              <Route path="episodic" element={<MemoryEpisodic />} />
              <Route path="working" element={<MemoryWorking />} />
            </Route>
            <Route path="commit-log" element={<CommitLog />} />
            <Route path="proposals" element={<Proposals />} />
            <Route path="code" element={<Code />} />
            <Route path="keys" element={<Keys />} />
          </Route>
          <Route path="/console" element={<ConsoleLayout />}>
            <Route index element={<Navigate to="operators" replace />} />
            <Route path="operators" element={<Operators />} />
            <Route path="keys" element={<AllKeys />} />
            <Route path="audit" element={<AuditLog />} />
            <Route path="jobs" element={<Jobs />} />
          </Route>
        </Route>
        <Route path="*" element={<Navigate to="/teams" replace />} />
      </Routes>
    </>
  );
}
