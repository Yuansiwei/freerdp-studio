using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Forms;

// StudioRdp: 专用 RDP 客户端启动器。
// 不带参数时弹出地址选择框（带历史），也可以直接用命令行指定目标。
// 远程桌面本身是 FreeRDP 原生界面，本程序不介入。
internal static class StudioRdp
{
    private const string DefaultServer = "192.168.98.184";
    private const int DefaultPort = 3389;
    private const int ActivationWindowMs = 30000;
    private const int HistoryLimit = 10;

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int MessageBoxW(IntPtr owner, string text, string caption, uint type);

    private static string _root;
    private static string _client;
    private static string _log;
    private static string _history;

    [STAThread]
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
        _root = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
        _client = FindClient(_root);
        _log = Path.Combine(_root, "client.stderr.log");
        _history = Path.Combine(_root, "servers.txt");

        if (_client == null)
        {
            Fail("找不到 wfreerdp.exe。请把本程序放在客户端目录，或放在含有 runtime\\wfreerdp.exe 的目录里。");
            return 2;
        }

        string server = null;
        int port = DefaultPort;
        string user = null;
        int scale = 0;
        int attempts = 1;

        for (int i = 0; i < args.Length; i++)
        {
            string arg = args[i];
            if (arg == "--user") { if (++i < args.Length) user = args[i]; }
            else if (arg == "--scale") { if (++i < args.Length) int.TryParse(args[i], out scale); }
            else if (arg == "--attempts") { if (++i < args.Length) int.TryParse(args[i], out attempts); }
            else if (arg == "--help" || arg == "-h")
            {
                MessageBoxW(IntPtr.Zero,
                    "用法：StudioRdp.exe [服务器[:端口]] [--user 用户名] [--scale 100-500] [--attempts 次数]\n\n"
                    + "不带参数时弹出地址选择框，连过的地址会被记住。",
                    "StudioRdp", 0x40);
                return 0;
            }
            else if (!arg.StartsWith("--"))
            {
                server = arg;
            }
        }

        if (attempts < 1) attempts = 1;
        if (attempts > 5) attempts = 5;

        if (string.IsNullOrEmpty(server))
        {
            server = AskServer();
            if (string.IsNullOrEmpty(server))
                return 0;
        }

        string host;
        int chosen = port;
        SplitServer(server, ref chosen, out host);
        if (string.IsNullOrEmpty(host))
        {
            Fail("地址无效：" + server);
            return 4;
        }
        port = chosen;

        Remember(host + (port == DefaultPort ? string.Empty : ":" + port));

        string endpoint = host.IndexOf(':') >= 0 ? "[" + host + "]:" + port : host + ":" + port;
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
            if (attempt < attempts) Thread.Sleep(2000);
        }

        Fail("连接失败：远端会话没有在 " + (ActivationWindowMs / 1000) + " 秒内激活。\n\n"
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

    // ---- 地址选择框与历史 ----

    private static string AskServer()
    {
        using (var form = new Form())
        {
            form.Text = "连接远程桌面";
            form.FormBorderStyle = FormBorderStyle.FixedDialog;
            form.StartPosition = FormStartPosition.CenterScreen;
            form.MaximizeBox = false;
            form.MinimizeBox = false;
            form.ClientSize = new Size(430, 128);
            form.TopMost = true;

            var label = new Label
            {
                Text = "地址（可带端口）：",
                Location = new Point(16, 14),
                Size = new Size(390, 20),
            };

            var combo = new ComboBox
            {
                Location = new Point(16, 36),
                Size = new Size(398, 26),
                DropDownStyle = ComboBoxStyle.DropDown,
            };
            combo.Items.AddRange(LoadHistory().ToArray());
            combo.Text = combo.Items.Count > 0 ? (string)combo.Items[0] : DefaultServer;

            var ok = new Button { Text = "连接", DialogResult = DialogResult.OK, Location = new Point(256, 80), Size = new Size(74, 30) };
            var cancel = new Button { Text = "取消", DialogResult = DialogResult.Cancel, Location = new Point(340, 80), Size = new Size(74, 30) };

            form.Controls.Add(label);
            form.Controls.Add(combo);
            form.Controls.Add(ok);
            form.Controls.Add(cancel);
            form.AcceptButton = ok;
            form.CancelButton = cancel;

            if (form.ShowDialog() != DialogResult.OK)
                return null;

            string value = combo.Text.Trim();
            return value.Length == 0 ? null : value;
        }
    }

    private static List<string> LoadHistory()
    {
        var list = new List<string>();
        try
        {
            if (!File.Exists(_history)) return list;
            foreach (string line in File.ReadAllLines(_history))
            {
                string value = line.Trim();
                if (value.Length > 0 && !list.Contains(value)) list.Add(value);
                if (list.Count >= HistoryLimit) break;
            }
        }
        catch { /* 历史读不出来不影响启动 */ }
        return list;
    }

    private static void Remember(string value)
    {
        if (string.IsNullOrEmpty(value)) return;
        try
        {
            var list = LoadHistory();
            list.RemoveAll(existing => string.Equals(existing, value, StringComparison.OrdinalIgnoreCase));
            list.Insert(0, value);
            if (list.Count > HistoryLimit) list.RemoveRange(HistoryLimit, list.Count - HistoryLimit);
            File.WriteAllLines(_history, list.ToArray(), Encoding.UTF8);
        }
        catch { /* 记不住历史不影响启动 */ }
    }

    // ---- 客户端进程 ----

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
