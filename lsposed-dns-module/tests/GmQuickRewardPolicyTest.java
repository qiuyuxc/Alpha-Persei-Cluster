import io.github.qiuyuxc.agdns.GmQuickRewardPolicy;

public final class GmQuickRewardPolicyTest {
    public static void main(String[] args) {
        String[] coins={"艾因索菲币","金币"};
        String[] accepted={"金币","艾因索菲币"," 金币 "};
        String[] rejected={null,"移转之花","金币礼包","金币兑换券","高级经验芯片"};
        int cases=0;
        for(String name:accepted){if(!GmQuickRewardPolicy.preferred(name,coins))throw new AssertionError("Coin not found");cases++;}
        for(String name:rejected){if(GmQuickRewardPolicy.preferred(name,coins))throw new AssertionError("Wrong item selected");cases++;}
        if(GmQuickRewardPolicy.amount(10000,1000000)!=10000)throw new AssertionError("Wrong preset amount");cases++;
        if(GmQuickRewardPolicy.amount(1000,200)!=200)throw new AssertionError("Limit ignored");cases++;
        for(long n:new long[]{0,-1}){try{GmQuickRewardPolicy.amount(n,100);throw new AssertionError("Invalid amount accepted");}catch(IllegalArgumentException expected){}cases++;}
        try{GmQuickRewardPolicy.amount(100,0);throw new AssertionError("Unavailable reward accepted");}catch(IllegalArgumentException expected){}cases++;
        System.out.println("GM quick reward selection: "+cases+" cases passed");
    }
}
