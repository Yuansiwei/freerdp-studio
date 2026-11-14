using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

// StudioRdp: 专用 RDP 客户端启动器（成品 exe，无自制界面）。
// 只负责拉起 FreeRDP 原生客户端、失败重试、日志落盘，并在会话期间后台驻留。
internal static class StudioRdp
{
    private const string DefaultServer = "192.168.98.184";
    private const int DefaultPort = 3389;
    private const int ActivationWindowMs = 30000;
    private const int RetryDelayMs = 2000;

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int MessageBoxW(IntPtr owner, string text, string caption, uint type);

    private static string _client;
    private static string _log;

    private static int Main(string[] args)
    {
        try
        {
            return Run(args);
        }
        catch (Exception error)
        {
            Fail("启动失败：" + error.Message);
            return 1;
        }
    }

    private static int Run(string[] args)
    {
        string root = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
        _client = FindClient(root);
        _log = Path.Combine(root, "client.stderr.log");

        if (_client == null)
        {
            Fail("找不到 wfreerdp.exe。请把本程序放在客户端目录，或放在含有 runtime\\wfreerdp.exe 的目录里。");
            return 2;
        }

        string server = DefaultServer;
        int port = DefaultPort;
        string user = null;
        int scale = 0;
        int attempts = 3;

        for (int i = 0; i < args.Length; i++)
        {
            string arg = args[i];
            if (arg == "--user") { if (++i < args.Length) user = args[i]; }
            else if (arg == "--scale") { if (++i < args.Length) int.TryParse(args[i], out scale); }
            else if (arg == "--attempts") { if (++i < args.Length) int.TryParse(args[i], out attempts); }
            else if (!arg.StartsWith("--"))
            {
                string host;
                int value = port;
                SplitServer(arg, ref value, out host);
                if (!string.IsNullOrEmpty(host)) { server = host; port = value; }
            }
        }

        if (attempts < 1) attempts = 1;
        if (attempts > 5) attempts = 5;

        string endpoint = server.IndexOf(':') >= 0 ? "[" + server + "]:" + port : server + ":" + port;
        string logTail = null;

        for (int attempt = 1; attempt <= attempts; attempt++)
        {
            var pump = new LogPump(_log);
            Process process = StartClient(BuildOptions(endpoint, user, scale), pump);

            if (WaitForActivation(process, pump))
            {
                process.WaitForExit();
                return 0;
            }

            logTail = pump.Tail(12);
            TryKill(process);
            if (attempt < attempts) Thread.Sleep(RetryDelayMs);
        }

        Fail("连接失败：远端会话没有在 " + (ActivationWindowMs / 1000) + " 秒内激活，已重试 " + attempts + " 次。\n\n"
             + (logTail ?? "（无日志）"));
        return 3;
    }

    private static void SplitServer(string value, ref int port, out string host)
    {
        host = (value ?? string.Empty).Trim();
        if (host.Length == 0) return;

        int colon = host.LastIndexOf(':');
        if (colon > 0 && colon < host.Length - 1)
        {
            string tail = host.Substring(colon + 1);
            if (IsDigits(tail))
            {
                port = int.Parse(tail);
                host = host.Substring(0, colon);
            }
        }
    }

    private static bool IsDigits(string value)
    {
        if (value.Length == 0) return false;
        foreach (char c in value)
            if (!char.IsDigit(c)) return false;
        return true;
    }

    private static string FindClient(string root)
    {
        string[] candidates =
        {
            Path.Combine(root, "wfreerdp.exe"),
            Path.Combine(root, "runtime", "wfreerdp.exe"),
            Path.Combine(root, "release", "studio-1.0", "wfreerdp.exe"),
        };
        foreach (string candidate in candidates)
            if (File.Exists(candidate)) return candidate;
        return null;
    }

    private static List<string> BuildOptions(string endpoint, string user, int scale)
    {
        var options = new List<string>
        {
            "/v:" + endpoint,
            "/f",
            "+toggle-fullscreen",
            "+dynamic-resolution",
            "+auto-reconnect",
            "/auto-reconnect-max-retries:10",
            "/gdi:sw",
            "/bpp:32",
            "/kbd:unicode:on",
            "/proxy:ignore",
            "/cert:ignore",
            "/log-level:WARN",
        };
        if (scale > 0) options.Add("/scale-desktop:" + scale);
        if (!string.IsNullOrEmpty(user)) options.Add("/u:\"" + user + "\"");
        return options;
    }

    private static string Quote(List<string> options)
    {
        var sb = new StringBuilder();
        foreach (string option in options)
        {
            if (sb.Length > 0) sb.Append(' ');
            if (option.IndexOf('"') >= 0) sb.Append(option);
            else if (option.IndexOf(' ') >= 0) sb.Append('"').Append(option).Append('"');
            else sb.Append(option);
        }
        return sb.ToString();
    }

    private static Process StartClient(List<string> options, LogPump pump)
    {
        try { if (File.Exists(_log)) File.Delete(_log); }
        catch { /* 日志被占用时继续 */ }

        var info = new ProcessStartInfo
        {
            FileName = _client,
            Arguments = Quote(options),
            WorkingDirectory = Path.GetDirectoryName(_client),
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardError = true,
            RedirectStandardOutput = true,
        };

        var process = new Process { StartInfo = info, EnableRaisingEvents = true };
        process.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs e) { if (e.Data != null) pump.Add(e.Data); };
        process.OutputDataReceived += delegate(object sender, DataReceivedEventArgs e) { if (e.Data != null) pump.Add(e.Data); };
        process.Start();
        process.BeginErrorReadLine();
        process.BeginOutputReadLine();
        return process;
    }

    private static bool WaitForActivation(Process process, LogPump pump)
    {
        var watch = Stopwatch.StartNew();
        while (watch.ElapsedMilliseconds < ActivationWindowMs)
        {
            if (process.HasExited) return false;
            if (pump.TimedOut()) return false;
            Thread.Sleep(200);
        }
        return !process.HasExited;
    }

    private static void TryKill(Process process)
    {
        try
        {
            if (!process.HasExited) process.Kill();
            process.WaitForExit(3000);
        }
        catch { /* 已经退出 */ }
    }

    private static void Fail(string text)
    {
        MessageBoxW(IntPtr.Zero, text, "StudioRdp", 0x10);
    }

    // 把客户端输出写入日志，并保留尾部若干行用于报错。
    private sealed class LogPump
    {
        private readonly object _gate = new object();
        private readonly string _path;
        private readonly StringBuilder _tail = new StringBuilder();
        private bool _timedOut;

        public LogPump(string path) { _path = path; }

        public void Add(string line)
        {
            lock (_gate)
            {
                try { File.AppendAllText(_path, line + Environment.NewLine, Encoding.UTF8); }
                catch { /* 写日志失败不影响会话 */ }

                _tail.AppendLine(line);
                if (_tail.Length > 16384) _tail.Remove(0, _tail.Length - 16384);
                if (line.IndexOf("ACTIVATION_TIMEOUT", StringComparison.Ordinal) >= 0) _timedOut = true;
            }
        }

        public bool TimedOut()
        {
            lock (_gate) return _timedOut;
        }

        public string Tail(int lines)
        {
            lock (_gate)
            {
                string[] all = _tail.ToString().Split(new[] { '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries);
                int start = Math.Max(0, all.Length - lines);
                return string.Join(Environment.NewLine, all, start, all.Length - start);
            }
        }
    }
}
