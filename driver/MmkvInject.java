import com.sun.jdi.Bootstrap;
import com.sun.jdi.ClassType;
import com.sun.jdi.Location;
import com.sun.jdi.Method;
import com.sun.jdi.ObjectReference;
import com.sun.jdi.ReferenceType;
import com.sun.jdi.StringReference;
import com.sun.jdi.ThreadReference;
import com.sun.jdi.Value;
import com.sun.jdi.VirtualMachine;
import com.sun.jdi.connect.AttachingConnector;
import com.sun.jdi.connect.Connector;
import com.sun.jdi.event.BreakpointEvent;
import com.sun.jdi.event.Event;
import com.sun.jdi.event.EventQueue;
import com.sun.jdi.request.BreakpointRequest;
import com.sun.jdi.request.EventRequestManager;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Map;

/**
 * 通过 JDWP 在 App 进程内调用 com.ammarahmed.mmkv.MMKV 写入 key/value。
 * ART 只允许在断点事件上下文里执行方法调用, 所以在 android.util.Log.println 下断点,
 * 命中后再调 MMKV.mmkvWithID / encode / decodeString。
 *
 * 用法: java MmkvInject <host> <port> <mmkvID> <rootDir> <key> <value>
 */
public class MmkvInject {
    static final String TARGET = "com.ammarahmed.mmkv.MMKV";

    public static void main(String[] argv) throws Exception {
        String host = argv[0];
        int port = Integer.parseInt(argv[1]);
        String mmkvId = argv[2], root = argv[3], key = argv[4], value = argv[5];

        AttachingConnector conn = null;
        for (AttachingConnector c : Bootstrap.virtualMachineManager().attachingConnectors()) {
            if (c.name().equals("com.sun.jdi.SocketAttach")) conn = c;
        }
        if (conn == null) throw new RuntimeException("no SocketAttach connector");
        Map<String, Connector.Argument> cargs = conn.defaultArguments();
        cargs.get("hostname").setValue(host);
        cargs.get("port").setValue(String.valueOf(port));
        if (cargs.containsKey("timeout")) cargs.get("timeout").setValue("30000");
        VirtualMachine vm = conn.attach(cargs);
        System.out.println("[jdi] attached");

        EventRequestManager erm = vm.eventRequestManager();
        BreakpointRequest bp = null;
        try {
            List<ReferenceType> logs = vm.classesByName("android.util.Log");
            if (logs.isEmpty()) throw new RuntimeException("android.util.Log not loaded");
            ClassType logCls = (ClassType) logs.get(0);
            Method target = null;
            for (Method m : logCls.methodsByName("println")) {
                if (m.argumentTypeNames().equals(Arrays.asList("int", "java.lang.String", "java.lang.String"))) {
                    target = m;
                }
            }
            if (target == null) throw new RuntimeException("Log.println(int,String,String) not found");
            Location loc = target.location();
            bp = erm.createBreakpointRequest(loc);
            bp.setSuspendPolicy(BreakpointRequest.SUSPEND_EVENT_THREAD);
            bp.enable();
            System.out.println("[jdi] breakpoint at " + loc);

            ThreadReference thread = null;
            EventQueue q = vm.eventQueue();
            long deadline = System.currentTimeMillis() + 90000;
            while (System.currentTimeMillis() < deadline && thread == null) {
                Event ev = q.remove(2000);
                if (ev == null) continue;
                if (ev instanceof BreakpointEvent) {
                    thread = ((BreakpointEvent) ev).thread();
                    System.out.println("[jdi] breakpoint hit thread=" + thread.name()
                            + " suspended=" + thread.isSuspended());
                    continue;
                }
                vm.resume();
            }
            if (thread == null) throw new RuntimeException("no breakpoint hit in 90s");

            bp.disable();
            erm.deleteEventRequest(bp);
            bp = null;

            ClassType ct = null;
            for (int i = 0; i < 12 && ct == null; i++) {
                List<ReferenceType> t = vm.classesByName(TARGET);
                if (!t.isEmpty()) ct = (ClassType) t.get(0);
                else Thread.sleep(2000);
            }
            if (ct == null) throw new RuntimeException("MMKV class not loaded");

            Method factory = null;
            for (Method m : ct.methodsByName("mmkvWithID")) {
                if (m.argumentTypeNames().equals(Arrays.asList("java.lang.String", "int", "java.lang.String"))) {
                    factory = m;
                    break;
                }
            }
            if (factory == null) throw new RuntimeException("mmkvWithID(String,int,String) not found");
            List<Value> fa = new ArrayList<>();
            fa.add(vm.mirrorOf(mmkvId));
            fa.add(vm.mirrorOf(1));
            fa.add(vm.mirrorOf(root));
            ObjectReference inst = (ObjectReference) ct.invokeMethod(thread, factory, fa,
                    ObjectReference.INVOKE_SINGLE_THREADED);
            System.out.println("[jdi] instance: " + inst);

            Method encode = null;
            for (Method m : ct.methodsByName("encode")) {
                if (m.argumentTypeNames().equals(Arrays.asList("java.lang.String", "java.lang.String"))) {
                    encode = m;
                    break;
                }
            }
            if (encode == null) throw new RuntimeException("encode(String,String) not found");
            List<Value> ea = new ArrayList<>();
            ea.add(vm.mirrorOf(key));
            ea.add(vm.mirrorOf(value));
            Value r = inst.invokeMethod(thread, encode, ea, ObjectReference.INVOKE_SINGLE_THREADED);
            System.out.println("[jdi] encode -> " + r);

            Method decode = null;
            for (Method m : ct.methodsByName("decodeString")) {
                if (m.argumentTypeNames().equals(Arrays.asList("java.lang.String", "java.lang.String"))) {
                    decode = m;
                    break;
                }
            }
            if (decode != null) {
                List<Value> da = new ArrayList<>();
                da.add(vm.mirrorOf(key));
                da.add(vm.mirrorOf(""));
                Value got = inst.invokeMethod(thread, decode, da, ObjectReference.INVOKE_SINGLE_THREADED);
                String s = (got instanceof StringReference) ? ((StringReference) got).value() : String.valueOf(got);
                System.out.println("[jdi] readback " + key + " = " + s);
            }
        } finally {
            try { if (bp != null) bp.disable(); } catch (Exception ignore) {}
            try { vm.resume(); } catch (Exception ignore) {}
            try { vm.dispose(); } catch (Exception ignore) {}
        }
    }
}
