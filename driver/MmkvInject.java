import com.sun.jdi.Bootstrap;
import com.sun.jdi.ClassLoaderReference;
import com.sun.jdi.ClassType;
import com.sun.jdi.Method;
import com.sun.jdi.ObjectReference;
import com.sun.jdi.ReferenceType;
import com.sun.jdi.StringReference;
import com.sun.jdi.ThreadReference;
import com.sun.jdi.Value;
import com.sun.jdi.VirtualMachine;
import com.sun.jdi.connect.AttachingConnector;
import com.sun.jdi.connect.Connector;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Map;

/**
 * 通过 JDWP 在 App 进程内调用 com.ammarahmed.mmkv.MMKV.encode(key, value)
 * 用法: java MmkvInject <host> <port> <mmkvID> <rootDir> <key> <value>
 */
public class MmkvInject {
    static ThreadReference pickThread(VirtualMachine vm) {
        ThreadReference main = null, any = null;
        for (ThreadReference t : vm.allThreads()) {
            if (!t.isSuspended()) continue;
            if (any == null) any = t;
            if ("main".equals(t.name())) { main = t; break; }
        }
        ThreadReference r = (main != null) ? main : any;
        if (r == null) throw new RuntimeException("no suspended thread");
        try {
            r.suspend();
        } catch (Exception e) {
            System.out.println("[jdi] thread.suspend 失败: " + e);
        }
        System.out.println("[jdi] picked " + r.name() + " suspended=" + r.isSuspended()
                           + " count=" + (r.isSuspended() ? r.suspendCount() : -1));
        return r;
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
        ThreadReference thread0 = pickThread(vm);
        try {
            List<ReferenceType> types = vm.classesByName("com.ammarahmed.mmkv.MMKV");
            for (int i = 0; i < 12 && types.isEmpty(); i++) {
                Thread.sleep(5000);
                types = vm.classesByName("com.ammarahmed.mmkv.MMKV");
            }
            if (types.isEmpty()) {
                System.out.println("[jdi] 类未加载，用 App classloader 强制 Class.forName");
                List<ReferenceType> mods = vm.classesByName("com.ammarahmed.mmkv.RNMMKVModule");
                if (mods.isEmpty()) throw new RuntimeException("RNMMKVModule 也未加载，无法取得 classloader");
                ClassLoaderReference loader = ((ClassType) mods.get(0)).classLoader();
                ClassType classCls = (ClassType) vm.classesByName("java.lang.Class").get(0);
                Method forName3 = null;
                for (Method m : classCls.methodsByName("forName")) {
                    if (m.argumentTypeNames().equals(Arrays.asList(
                            "java.lang.String", "boolean", "java.lang.ClassLoader"))) {
                        forName3 = m;
                    }
                }
                if (forName3 == null) throw new RuntimeException("Class.forName(String,boolean,ClassLoader) 未找到");
                List<Value> fa0 = new ArrayList<>();
                fa0.add(vm.mirrorOf("com.ammarahmed.mmkv.MMKV"));
                fa0.add(vm.mirrorOf(true));
                fa0.add(loader);
                Value loaded = classCls.invokeMethod(thread0, forName3, fa0, ObjectReference.INVOKE_SINGLE_THREADED);
                System.out.println("[jdi] forName -> " + loaded);
                types = vm.classesByName("com.ammarahmed.mmkv.MMKV");
            }
            System.out.println("[jdi] class loaded: " + types.size());
            if (types.isEmpty()) {
                for (ReferenceType t : vm.allClasses()) {
                    if (t.name().contains("mmkv") || t.name().contains("MMKV")) System.out.println("[jdi] loaded: " + t.name());
                }
                throw new RuntimeException("MMKV class not loaded in target process");
            }
            ClassType ct = (ClassType) types.get(0);
            thread0 = pickThread(vm);
            ThreadReference thread = thread0;
            System.out.println("[jdi] thread=" + thread.name());

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
            ObjectReference inst = (ObjectReference) ct.invokeMethod(thread, factory, fa, ObjectReference.INVOKE_SINGLE_THREADED);
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
            System.out.println("[jdi] encode result: " + r);

            // 回读确认
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
            try { vm.resume(); } catch (Exception ignore) {}
            try { vm.dispose(); } catch (Exception ignore) {}
        }
    }
}
