using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
internal static class SendRdpInput
{
    delegate bool EnumProc(IntPtr window, IntPtr unused);
    [StructLayout(LayoutKind.Sequential)]
    struct CopyData { public UIntPtr Kind; public uint Size; public IntPtr Data; }
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc callback, IntPtr unused);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetWindowText(IntPtr window, StringBuilder title, int size);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr window, out uint pid);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern short GetAsyncKeyState(int key);
    [DllImport("user32.dll", SetLastError=true)]
    static extern IntPtr SendMessageTimeout(IntPtr window, uint msg, IntPtr sender, ref CopyData data, uint flags, uint timeout, out UIntPtr result);
    static readonly Dictionary<string,uint> Keys = new Dictionary<string,uint>(StringComparer.OrdinalIgnoreCase) {
        {"CTRL",0x1D}, {"CONTROL",0x1D}, {"SHIFT",0x2A}, {"ALT",0x38},
        {"ENTER",0x1C}, {"TAB",0x0F}, {"ESC",0x01}, {"BACKSPACE",0x0E},
        {"SPACE",0x39}, {"F15",0x66}, {"HOME",0x01000047}, {"END",0x0100004F},
        {"LEFT",0x0100004B}, {"RIGHT",0x0100004D}, {"UP",0x01000048}, {"DOWN",0x01000050},
        {"A",0x1E}, {"B",0x30}, {"C",0x2E}, {"D",0x20}, {"E",0x12}, {"F",0x21},
        {"G",0x22}, {"H",0x23}, {"I",0x17}, {"J",0x24}, {"K",0x25}, {"L",0x26},
        {"M",0x32}, {"N",0x31}, {"O",0x18}, {"P",0x19}, {"Q",0x10}, {"R",0x13},
        {"S",0x1F}, {"T",0x14}, {"U",0x16}, {"V",0x2F}, {"W",0x11}, {"X",0x2D},
        {"Y",0x15}, {"Z",0x2C}
    };
    static int Main(string[] args)
    {
        try {
            if(args.Length!=4 || args[0]!="--target" || (args[2]!="--text" && args[2]!="--key" && args[2]!="--status" && args[2]!="--keepalive")) {
                Console.Error.WriteLine("Usage: SendRdpInput --target HOST:PORT --text \"text\" | --key CTRL+A | --status query"); return 2;
            }
            // The client window is titled with the host only (no port, no prefix).
            string titleWanted=args[1];
            if(!titleWanted.StartsWith("[") && titleWanted.LastIndexOf(':')>0)
                titleWanted=titleWanted.Substring(0,titleWanted.LastIndexOf(':'));
            else if(titleWanted.StartsWith("[") && titleWanted.Contains("]:"))
                titleWanted=titleWanted.Substring(1,titleWanted.IndexOf("]:")-1);
            byte[] payload;
            uint kind;
            if(args[2]=="--keepalive") {payload=new byte[4];kind=0x46524433;}
            else if(args[2]=="--status") {payload=new byte[4];kind=0x46524430;}
            else if(args[2]=="--text") {
                if(args[3].Length==0 || args[3].Length>4096 || args[3].IndexOfAny(new[]{'\0','\r','\n','\t'})>=0)
                    throw new ArgumentException("Use nonempty text up to 4096 UTF-16 units. Send control keys separately.");
                payload=Encoding.Unicode.GetBytes(args[3]); kind=0x46524431;
            } else {
                string[] parts=args[3].Split('+');
                if(parts.Length==0 || parts.Length>4) throw new ArgumentException("Invalid key combination.");
                var events=new List<uint>();
                var held=new HashSet<uint>();
                foreach(string part in parts) {
                    uint key;
                    if(!Keys.TryGetValue(part,out key) || !held.Add(key)) throw new ArgumentException("Unsupported or duplicate key.");
                    events.Add(key);
                }
                int count=events.Count;
                for(int i=count-1;i>=0;i--) events.Add(events[i]|0x80000000);
                payload=new byte[events.Count*4];
                for(int i=0;i<events.Count;i++) BitConverter.GetBytes(events[i]).CopyTo(payload,i*4);
                kind=0x46524432;
            }
            var windows=new List<IntPtr>();
            EnumWindows(delegate(IntPtr window,IntPtr unused) {
                var title=new StringBuilder(256); GetWindowText(window,title,title.Capacity);
                if(!string.Equals(title.ToString(),titleWanted,StringComparison.OrdinalIgnoreCase)) return true;
                uint pid; GetWindowThreadProcessId(window,out pid);
                using(var process=Process.GetProcessById((int)pid)) {
                    // Accept the client next to this tool or in a runtime subfolder.
                    string baseDir=AppDomain.CurrentDomain.BaseDirectory;
                    string actual=process.MainModule.FileName;
                    string beside=System.IO.Path.Combine(baseDir,"wfreerdp.exe");
                    string underRuntime=System.IO.Path.Combine(baseDir,"runtime","wfreerdp.exe");
                    if(string.Equals(actual,beside,StringComparison.OrdinalIgnoreCase) ||
                       string.Equals(actual,underRuntime,StringComparison.OrdinalIgnoreCase))
                        windows.Add(window);
                }
                return true;
            },IntPtr.Zero);
            if(windows.Count!=1) throw new InvalidOperationException("Expected exactly one matching client. Start Start-FreeRdp.ps1 and finish login first.");
            foreach(int modifier in new[]{0x10,0x11,0x12})
                if((GetAsyncKeyState(modifier)&0x8000)!=0) throw new InvalidOperationException("Release local Shift, Ctrl and Alt before sending input.");
            IntPtr before=GetForegroundWindow();
            IntPtr memory=Marshal.AllocHGlobal(payload.Length);
            try {
                Marshal.Copy(payload,0,memory,payload.Length);
                var data=new CopyData{Kind=(UIntPtr)kind,Size=(uint)payload.Length,Data=memory};
                UIntPtr ack;
                if(SendMessageTimeout(windows[0],0x004A,IntPtr.Zero,ref data,0x22,15000,out ack)==IntPtr.Zero)
                    throw new InvalidOperationException("Input call failed or timed out. Outcome may be partial. Check remote screen before retrying. Win32="+Marshal.GetLastWin32Error());
                ulong result=ack.ToUInt64();
                if(args[2]=="--keepalive") {
                    if(result<1000) throw new InvalidOperationException("Keepalive endpoint unavailable.");
                    Console.WriteLine("IntervalSeconds=120 F15PairsSent="+(result-1000));
                    return 0;
                }
                if(args[2]=="--status") {
                    if(result<1000 || result>1007) throw new InvalidOperationException("Status endpoint unavailable.");
                    Console.WriteLine("Connected="+(((result-1000)&1)!=0)+" Unicode="+(((result-1000)&2)!=0)+" InputSuspended="+(((result-1000)&4)!=0)+" TargetInBackground="+(before!=windows[0]));
                    return 0;
                }
                if(result==2) throw new InvalidOperationException("Server did not negotiate Unicode input. No text was sent.");
                if(result!=1) throw new InvalidOperationException("Client rejected input. Check remote screen before retrying.");
                Console.WriteLine("RDP client accepted input. TargetInBackground="+(before!=windows[0])+" ForegroundUnchanged="+(before==GetForegroundWindow())+". Verify delivery on remote screen.");
            } finally {Marshal.FreeHGlobal(memory);}
            return 0;
        }catch(Exception error){Console.Error.WriteLine(error.Message);return 1;}
    }
}

