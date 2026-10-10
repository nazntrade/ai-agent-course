plugins { id("com.android.application") }
val privatechatTestTarget = providers.gradleProperty("privatechat.testTarget").getOrElse("e2e")
require(privatechatTestTarget in setOf("e2e", "normal")) { "Unknown privatechat.testTarget" }
android {
    namespace = "com.example.privatechat"
    compileSdk { version = release(37) { minorApiLevel = 0 } }
    buildToolsVersion = "36.0.0"
    buildFeatures { buildConfig = true }
    defaultConfig {
        applicationId = "com.example.privatechat"
        minSdk = 26
        targetSdk = 36
        versionCode = 1
        versionName = "1.0"
        testInstrumentationRunner = "com.example.privatechat.ProbeInstrumentation"
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    flavorDimensions += "transport"
    productFlavors {
        create("lan") { dimension = "transport" }
        create("https") { dimension = "transport" }
    }
    buildTypes {
        getByName("debug") { isDebuggable = true }
        create("e2e") {
            initWith(getByName("debug"))
            applicationIdSuffix = ".e2e"
            signingConfig = signingConfigs.getByName("debug")
            matchingFallbacks += "debug"
        }
    }
    testBuildType = if (privatechatTestTarget == "e2e") "e2e" else "debug"
}
dependencies {
    implementation("androidx.activity:activity-ktx:1.12.4")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.9.4")
    implementation("androidx.lifecycle:lifecycle-viewmodel-ktx:2.9.4")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
}
dependencyLocking { lockAllConfigurations() }
