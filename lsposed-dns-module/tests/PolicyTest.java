import io.github.qiuyuxc.agdns.DnsPolicy;
import java.nio.file.Files;
import java.nio.file.Paths;

public final class PolicyTest {
    public static void main(String[] args) throws Exception {
        if (DnsPolicy.matches(null)) throw new AssertionError("null hostname");
        int count = 0;
        for (String line : Files.readAllLines(Paths.get(args[0]))) {
            if (line.startsWith("#")) continue;
            String[] parts = line.split("\t", -1);
            boolean expected = parts[0].equals("1");
            if (DnsPolicy.matches(parts[1]) != expected) throw new AssertionError(line);
            count++;
        }
        System.out.println("Java domain policy: " + count + " cases passed");
    }
}
