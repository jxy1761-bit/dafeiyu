import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    id("com.android.library")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

// Runtime image is prepared outside Git; point this at the extracted 1.4.1 assets.
val runtimeAssetsDir = providers.gradleProperty("runtimeAssetsDir").orNull
    ?: "../../../.inputs/apk-assets"

android {
    namespace = "io.astrbot.android"
    compileSdk = 36

    defaultConfig {
        minSdk = 26
        buildConfigField("boolean", "BUNDLED_RUNTIME", "true")
        buildConfigField("boolean", "WEBCHAT_ONLY", "true")
        buildConfigField("String", "UPDATE_CATALOG_URL", "\"\"")
        buildConfigField("String", "NAPCAT_CATALOG_URL", "\"\"")
        buildConfigField("String", "MANAGER_CATALOG_URL", "\"\"")
        buildConfigField("String", "UPDATE_PUBLIC_KEY", "\"\"")
        buildConfigField("String", "APPLICATION_ID", "\"com.dafeiyu.dafeiyu_full\"")
        buildConfigField("String", "VERSION_NAME", "\"1.0.0\"")
        buildConfigField("int", "VERSION_CODE", "10000")
    }

    buildFeatures {
        compose = true
        buildConfig = true
        aidl = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    sourceSets["main"].assets.srcDir(file(runtimeAssetsDir))
    sourceSets["main"].jniLibs.srcDir("src/main/jniLibs")

    packaging {
        jniLibs.useLegacyPackaging = true
        resources.excludes += setOf("META-INF/DEPENDENCIES", "META-INF/LICENSE", "META-INF/NOTICE", "META-INF/INDEX.LIST")
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(JvmTarget.JVM_17)
    }
}

dependencies {
    implementation(platform("androidx.compose:compose-bom:2025.04.01"))
    implementation("androidx.activity:activity-compose:1.10.1")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.9.0")
    implementation("androidx.webkit:webkit:1.12.1")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.10.2")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("org.apache.commons:commons-compress:1.27.1")
    implementation("org.apache.sshd:sshd-core:2.20.0")
    implementation("org.slf4j:slf4j-nop:2.0.17")
    implementation("dev.rikka.shizuku:api:13.1.5")
    implementation("dev.rikka.shizuku:provider:13.1.5")
}
