plugins {
    id("com.android.application")
    id("dev.flutter.flutter-gradle-plugin")
}

android {
    namespace = "com.dafeiyu.dafeiyu_chat"
    compileSdk = 36
    ndkVersion = flutter.ndkVersion

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    defaultConfig {
        applicationId = "com.dafeiyu.dafeiyu_full"
        minSdk = 26
        targetSdk = 36
        versionCode = 10004
        versionName = "1.0.4"
    }

    buildTypes {
        release {
            signingConfig = signingConfigs.getByName("debug")
            // Manager deps use reflection (SSHD/Shizuku); keep the runtime intact.
            isMinifyEnabled = false
            isShrinkResources = false
        }
    }

    androidResources {
        noCompress += "bin"
    }

    packaging {
        jniLibs.useLegacyPackaging = true
        resources.merges += setOf("META-INF/DEPENDENCIES", "META-INF/LICENSE", "META-INF/NOTICE")
        resources.excludes += setOf("META-INF/INDEX.LIST", "META-INF/*.SF", "META-INF/*.DSA", "META-INF/*.RSA")
    }
}

kotlin {
    compilerOptions {
        jvmTarget = org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17
    }
}

flutter {
    source = "../.."
}

dependencies {
    implementation(project(":manager"))
}
