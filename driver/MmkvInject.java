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
 * 另外同步维护 react-native-mmkv-storage 的索引键 (stringIndex),
 * 否则 JS 侧 useMMKVStorage 的 getValueType 返回 null, hook 只会拿到 defaultValue。
 *
 * 用法: java MmkvInject <host> <port> <mmkvID> <rootDir> <key> <value>
 */
public class MmkvInject {
    static final String TARGET = "com.ammarahmed.mmkv.MMKV";

    static Method find(ClassType ct, String name, List<String> argTypes) {
        for (Method m : ct.methodsByName(name)) {
            if (m.argumentTypeNames().equals(argTypes)) return m;
        }
        return null;
    }

    static Value call(ThreadReference t, ObjectReference obj, Method m, List<Value> args) throws Exception {
        return obj.invokeMethod(t, m, args, ObjectReference.INVOKE_SINGLE_THREADED);
    }

    static String str(Value v) {
        if (v == null) return "null";
        if (v instanceof StringReference) return ((StringReference) v).value();
        return String.valueOf(v);
    }

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
            Method target = find(logCls, "println",
                    Arrays.asList("int", "java.lang.String", "java.lang.String"));
            if (target == null) throw new RuntimeException("Log.println(int,String,String) not found");
            Location loc = target.location();
            bp = erm.createBreakpointRequest(loc);
            bp.setSuspendPolicy(BreakpointRequest.SUSPEND_EVENT_THREAD);
            bp.enable();
            System.out.println("[jdi] breakpoint at " + loc);

            ThreadReference thread = null;
            EventQueue q = vm.eventQueue();
            long deadline = System.currentTimeMillis() + 45000;
            while (System.currentTimeMillis() < deadline && thread == null) {
                com.sun.jdi.event.EventSet es = q.remove(2000);
                if (es == null) continue;
                boolean hit = false;
                for (Event ev : es) {
                    if (ev instanceof BreakpointEvent) {
                        thread = ((BreakpointEvent) ev).thread();
                        System.out.println("[jdi] breakpoint hit thread=" + thread.name()
                                + " suspended=" + thread.isSuspended());
                        hit = true;
                    }
                }
                if (!hit) es.resume();
            }
            if (thread == null) throw new RuntimeException("no breakpoint hit in 45s");

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

            // 打印相关方法签名, 便于诊断
            for (Method m : ct.methods()) {
                String n = m.name();
                if (n.equals("encode") || n.equals("decodeStringSet") || n.equals("decodeString")
                        || n.equals("containsKey")) {
                    System.out.println("[jdi] method " + n + m.argumentTypeNames());
                }
            }

            Method factory = find(ct, "mmkvWithID",
                    Arrays.asList("java.lang.String", "int", "java.lang.String"));
            if (factory == null) throw new RuntimeException("mmkvWithID(String,int,String) not found");
            List<Value> fa = new ArrayList<>();
            fa.add(vm.mirrorOf(mmkvId));
            fa.add(vm.mirrorOf(1));
            fa.add(vm.mirrorOf(root));
            ObjectReference inst = (ObjectReference) ct.invokeMethod(thread, factory, fa,
                    ObjectReference.INVOKE_SINGLE_THREADED);
            System.out.println("[jdi] instance: " + inst);

            Method encode = find(ct, "encode", Arrays.asList("java.lang.String", "java.lang.String"));
            if (encode == null) throw new RuntimeException("encode(String,String) not found");
            List<Value> ea = new ArrayList<>();
            ea.add(vm.mirrorOf(key));
            ea.add(vm.mirrorOf(value));
            System.out.println("[jdi] encode -> " + call(thread, inst, encode, ea));

            Method decode = find(ct, "decodeString",
                    Arrays.asList("java.lang.String", "java.lang.String"));
            if (decode != null) {
                List<Value> da = new ArrayList<>();
                da.add(vm.mirrorOf(key));
                da.add(vm.mirrorOf(""));
                System.out.println("[jdi] readback " + key + " = " + str(call(thread, inst, decode, da)));
            }

            // ---- 维护索引: 让 stringIndex 集合包含该 key ----
            Method decSet = find(ct, "decodeStringSet", Arrays.asList("java.lang.String"));
            Method encSet = find(ct, "encode", Arrays.asList("java.lang.String", "java.util.Set"));
            if (decSet == null || encSet == null) {
                System.out.println("[jdi] index step skipped (decSet=" + (decSet != null)
                        + ", encSet=" + (encSet != null) + ")");
            } else {
                List<Value> dsa = new ArrayList<>();
                dsa.add(vm.mirrorOf("stringIndex"));
                ObjectReference set = (ObjectReference) call(thread, inst, decSet, dsa);
                System.out.println("[jdi] stringIndex before = " + (set == null ? "null" : set.toString()));
                if (set == null) {
                    List<ReferenceType> hs = vm.classesByName("java.util.HashSet");
                    System.out.println("[jdi] HashSet loaded=" + hs.size());
                    if (!hs.isEmpty()) {
                        ClassType hst = (ClassType) hs.get(0);
                        Method ctor = hst.concreteMethodByName("<init>", "()V");
                        if (ctor != null) {
                            set = hst.newInstance(thread, ctor, new ArrayList<Value>(),
                                    ObjectReference.INVOKE_SINGLE_THREADED);
                        }
                    }
                    System.out.println("[jdi] created set = " + set);
                }
                if (set != null) {
                    Method add = null;
                    for (Method m : set.referenceType().methodsByName("add")) {
                        if (m.argumentTypeNames().equals(Arrays.asList("java.lang.Object"))) add = m;
                    }
                    if (add != null) {
                        List<Value> aa = new ArrayList<>();
                        aa.add(vm.mirrorOf(key));
                        call(thread, set, add, aa);
                    }
                    List<Value> esa = new ArrayList<>();
                    esa.add(vm.mirrorOf("stringIndex"));
                    esa.add(set);
                    System.out.println("[jdi] encode stringIndex -> " + call(thread, inst, encSet, esa));
                    List<Value> dsa2 = new ArrayList<>();
                    dsa2.add(vm.mirrorOf("stringIndex"));
                    ObjectReference after = (ObjectReference) call(thread, inst, decSet, dsa2);
                    System.out.println("[jdi] stringIndex after = " + (after == null ? "null" : after.toString()));
                }
            }
        } finally {
            try { if (bp != null) bp.disable(); } catch (Exception ignore) {}
            try { vm.resume(); } catch (Exception ignore) {}
            try { vm.dispose(); } catch (Exception ignore) {}
        }
    }
}
