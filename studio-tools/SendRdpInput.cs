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
    const string Usage =
        "Usage: SendRdpInput --target HOST:PORT --text \"text\" | --key CTRL+A | --status query | --keepalive 1\n" +
        "       | --wheel -3 | --capture out.bmp\n" +
        "       | --move X,Y | --click X,Y | --rclick X,Y | --down X,Y | --up X,Y";

    // pointer action bits understood by kind 0x46524436
    const uint PTR_MOVE = 1, PTR_LEFT_DOWN = 2, PTR_LEFT_UP = 4, PTR_RIGHT_DOWN = 8, PTR_RIGHT_UP = 16;

    static bool TryPoint(string text, out int x, out int y)
    {
        x = 0; y = 0;
        string[] parts = text.Split(',');
        if (parts.Length != 2) return false;
        if (!int.TryParse(parts[0].Trim(), out x)) return false;
        if (!int.TryParse(parts[1].Trim(), out y)) return false;
        return x >= 0 && y >= 0 && x <= 65535 && y <= 65535;
    }

    static byte[] Le32(uint value)
    {
        byte[] b = new byte[4];
        BitConverter.GetBytes(value).CopyTo(b, 0);
        return b;
    }

    static int Main(string[] args)
    {
        try {
            var commands = new HashSet<string>(StringComparer.Ordinal) {
                "--text","--key","--status","--keepalive","--wheel","--capture",
                "--move","--click","--rclick","--down","--up"
            };
            if(args.Length!=4 || args[0]!="--target" || !commands.Contains(args[2])) {
                Console.Error.WriteLine(Usage); return 2;
            }
            // The client window is titled with the host only (no port, no prefix).
            string titleWanted=args[1];
            if(!titleWanted.StartsWith("[") && titleWanted.LastIndexOf(':')>0)
                titleWanted=titleWanted.Substring(0,titleWanted.LastIndexOf(':'));
            else if(titleWanted.StartsWith("[") && titleWanted.Contains("]:"))
                titleWanted=titleWanted.Substring(1,titleWanted.IndexOf("]:")-1);

            byte[] payload;
            uint kind;
            bool wantsStatus=false, wantsKeepalive=false;

            if(args[2]=="--keepalive") {payload=new byte[4];kind=0x46524433;wantsKeepalive=true;}
            else if(args[2]=="--status") {payload=new byte[4];kind=0x46524430;wantsStatus=true;}
            else if(args[2]=="--text") {
                if(args[3].Length==0 || args[3].Length>4096 || args[3].IndexOfAny(new[]{'\0','\r','\n','\t'})>=0)
                    throw new ArgumentException("Use nonempty text up to 4096 UTF-16 units. Send control keys separately.");
                payload=Encoding.Unicode.GetBytes(args[3]); kind=0x46524431;
            } else if(args[2]=="--key") {
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
            } else if(args[2]=="--wheel") {
                int notches;
                if(!int.TryParse(args[3].Trim(), out notches)) throw new ArgumentException("Wheel notches must be an integer.");
                if(notches==0 || notches<-50 || notches>50) throw new ArgumentException("Wheel notches must be within -50..50.");
                payload=Le32((uint)notches); kind=0x46524434;
            } else if(args[2]=="--capture") {
                if(args[3].Length==0) throw new ArgumentException("Capture needs an output .bmp path.");
                payload=Encoding.UTF8.GetBytes(args[3]); kind=0x46524435;
            } else {
                int x, y;
                if(!TryPoint(args[3], out x, out y)) throw new ArgumentException("Pointer arguments must be X,Y with non-negative integers.");
                uint action =
                    args[2]=="--move"   ? PTR_MOVE :
                    args[2]=="--click"  ? (PTR_LEFT_DOWN | PTR_LEFT_UP) :
                    args[2]=="--rclick" ? (PTR_RIGHT_DOWN | PTR_RIGHT_UP) :
                    args[2]=="--down"   ? PTR_LEFT_DOWN :
                                          PTR_LEFT_UP;
                payload=new byte[12];
                BitConverter.GetBytes(x).CopyTo(payload,0);
                BitConverter.GetBytes(y).CopyTo(payload,4);
                BitConverter.GetBytes(action).CopyTo(payload,8);
                kind=0x46524436;
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
                if(wantsKeepalive) {
                    if(result<1000) throw new InvalidOperationException("Keepalive endpoint unavailable.");
                    Console.WriteLine("IntervalSeconds=120 F15PairsSent="+(result-1000));
                    return 0;
                }
                if(wantsStatus) {
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
